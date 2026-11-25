r"""Écrit data/observatoire.json : la synthèse par source qu'affiche observatoire.html.

Trois sources, toutes en accès libre et légal :
  - France Travail     offres déposées directement sur francetravail.fr (origineOffre.origine = 1) ;
  - Sites partenaires  offres d'autres sites d'emploi (Indeed, Meteojob, DirectEmploi, PMEJob…)
                       que France Travail rediffuse dans son API (origine = 2, nom du site dans
                       origineOffre.partenaires) : aucune collecte en plus, elles sont déjà dans data/brut ;
  - Jobicy             offres en télétravail ouvertes aux candidats basés en France, via l'API publique
                       et sans clé de Jobicy (https://jobicy.com/jobs-rss-feed). Conditions : créditer
                       Jobicy avec un lien, renvoyer vers l'annonce d'origine, au plus un appel par heure.

Usage :
    .venv\Scripts\python.exe scripts\observatoire.py                 # synthèse + collecte Jobicy du jour
    .venv\Scripts\python.exe scripts\observatoire.py --sans-jobicy   # synthèse seule, sans appel réseau

Appelé automatiquement à la fin de scripts/resumer.py, donc chaque matin par l'Action « veille ».
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import re
import statistics
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

RACINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RACINE / "scripts"))
from resumer import salaire_min_max  # noqa: E402  (même lecture des salaires que la page principale)

SORTIE = RACINE / "data" / "observatoire.json"
DOSSIER_JOBICY = RACINE / "data" / "jobicy"

# Catégories Jobicy proches des métiers suivis (liste : https://jobicy.com/api/v2/remote-jobs?get=industries).
JOBICY_URL = "https://jobicy.com/api/v2/remote-jobs"
JOBICY_CATEGORIES = ["marketing", "seo", "copywriting", "design-multimedia"]
JOBICY_GEO = "france"
# La catégorie « Marketing & Sales » de Jobicy mélange marketing et vente : on ne garde que les
# intitulés qui relèvent des métiers suivis (marketing, contenu, communication, design numérique).
JOBICY_INTITULES = re.compile(
    r"marketing|market\b|content|contenu|copy|writer|r[ée]dact|\bseo\b|\bsea\b|brand|marque|growth"
    r"|acquisition|social|community|communication|\bcrm\b|e-?commerce|digital|num[ée]rique|media|m[ée]dia"
    r"|design|motion|video|vid[ée]o|\bux\b|\bui\b|\bcro\b", re.IGNORECASE)
USER_AGENT = "observatoire-emploi-etudiant (github.com/mylenemart/metier)"

CONTRATS = ["CDI", "CDD", "Alternance/Stage", "Autre"]
TOP_N = 15

# Noms lisibles des sites partenaires de France Travail ; les autres sont mis en forme automatiquement.
NOMS_SITES = {
    "INDEED": "Indeed", "METEOJOB": "Meteojob", "DIRECTEMPLOI": "DirectEmploi", "PMEJOB": "PMEJob",
    "LABONNEALTERNANCE": "La bonne alternance", "BEETWEEN": "Beetween", "HELLOWORK": "HelloWork",
    "OUESTFRANCE": "Ouest-France Emploi", "HANDICAP_JOB": "Handicap Job", "WE_RECRUIT": "We Recruit",
    "TALENTPLUG": "TalentPlug", "XTRAMILE": "Xtramile", "DISTRIJOB": "Distrijob", "BROADBEAN": "Broadbean",
    "JOBS THAT MAKE SENS": "Jobs That Make Sense", "DIGITALRECRUITERS": "Digitalrecruiters",
    "LINDUSTRIE RECRUTE": "L'Industrie recrute", "TALENTS_HANDICAP": "Talents Handicap",
}

TELETRAVAIL = re.compile(r"t[ée]l[ée][\s-]?travail|full[\s-]?remote|\bremote\b|hybride", re.IGNORECASE)
PAS_DE_TELETRAVAIL = re.compile(
    r"(pas|aucun|sans)\s+(de\s+|d[e']\s*)?(possibilit[ée]\s+de\s+)?t[ée]l[ée][\s-]?travail"
    r"|t[ée]l[ée][\s-]?travail\s*(:\s*)?(non|impossible|exclu)", re.IGNORECASE)


# ----------------------------------------------------------------------------- normalisation
def nettoyer_intitule(intitule):
    """'CHARGÉ DE MARKETING DIGITAL (H/F)' -> 'Chargé de marketing digital'."""
    t = html.unescape(intitule or "")
    t = re.sub(r"\(?\b[hfx]\s*/\s*[hfx](\s*/\s*[hfx])?\b\)?", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s{2,}", " ", t).strip(" -–—|,")
    if t.isupper():
        t = t.lower()
    return t[:1].upper() + t[1:]


def nom_site(nom):
    nom = (nom or "").strip()
    if not nom:
        return "Site partenaire"
    return NOMS_SITES.get(nom.upper()) or nom.replace("_", " ").title()


def contrat_france_travail(o):
    nature = (o.get("natureContrat") or "").lower()
    if o.get("alternance") or "apprentissage" in nature or "professionnalisation" in nature:
        return "Alternance/Stage"
    code = (o.get("typeContrat") or "").upper()
    if code in ("CDI", "DIN"):
        return "CDI"
    if code in ("CDD", "MIS", "SAI", "TTI", "CDS"):  # intérim et saisonnier : contrats courts
        return "CDD"
    return "Autre"


def salaire_annuel(o):
    smin, smax = salaire_min_max((o.get("salaire") or {}).get("libelle"))
    if smin is None:
        return None
    return (smin + (smax if smax is not None else smin)) / 2


def teletravail(texte):
    return bool(TELETRAVAIL.search(texte)) and not PAS_DE_TELETRAVAIL.search(texte)


# ----------------------------------------------------------------------------- France Travail
def offres_france_travail():
    """Offres actives du dernier jour extrait, chacune dans sa dernière version connue."""
    jours = sorted(f for f in (RACINE / "data" / "actives").glob("*.csv")
                   if re.fullmatch(r"\d{4}-\d{2}-\d{2}", f.stem))
    if not jours:
        raise SystemExit("Aucune extraction France Travail : lancez d'abord scripts/extraire.py")
    with jours[-1].open(encoding="utf-8") as f:
        ids_actifs = {r["id"] for r in csv.DictReader(f)}

    versions = {}
    for fichier in sorted((RACINE / "data" / "brut").glob("*/*.jsonl")):
        with fichier.open(encoding="utf-8") as fh:
            for ligne in fh:
                if ligne.strip():
                    v = json.loads(ligne)
                    if v["id"] in ids_actifs:
                        versions[v["id"]] = v

    offres = []
    for oid, v in versions.items():
        o = v["offre"]
        origine = o.get("origineOffre") or {}
        partenaires = origine.get("partenaires") or []
        partenaire = str(origine.get("origine")) == "2"
        texte = f"{o.get('intitule') or ''} {o.get('description') or ''}"
        offres.append({
            "id": oid,
            "source": "partenaires" if partenaire else "france_travail",
            "site": nom_site(partenaires[0].get("nom")) if partenaire and partenaires else
                    ("Site partenaire" if partenaire else "France Travail"),
            "intitule": nettoyer_intitule(o.get("intitule")),
            "contrat": contrat_france_travail(o),
            "salaire": salaire_annuel(o),
            "teletravail": teletravail(texte),
        })
    return jours[-1].stem, offres


# ----------------------------------------------------------------------------- Jobicy
def appel_jobicy(categorie):
    url = f"{JOBICY_URL}?count=100&geo={JOBICY_GEO}&industry={categorie}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8")).get("jobs") or []


def contrat_jobicy(types):
    """Jobicy publie un rythme (Full-Time, Part-Time…) plutôt qu'un contrat : seul le stage se rapproche."""
    t = " ".join(types or []).lower()
    return "Alternance/Stage" if "intern" in t else "Autre"


def collecter_jobicy(jour):
    """Interroge Jobicy (une requête par catégorie) et enregistre data/jobicy/<jour>.json."""
    offres, erreurs = {}, []
    for i, categorie in enumerate(JOBICY_CATEGORIES):
        if i:
            time.sleep(2)
        try:
            for j in appel_jobicy(categorie):
                smin, smax = j.get("annualSalaryMin"), j.get("annualSalaryMax")
                en_euros = (j.get("salaryCurrency") or "").upper() == "EUR"
                valeurs = [float(x) for x in (smin, smax) if isinstance(x, (int, float)) and x > 0]
                offres[str(j.get("id"))] = {
                    "id": str(j.get("id")),
                    "source": "jobicy",
                    "site": "Jobicy",
                    "intitule": nettoyer_intitule(j.get("jobTitle")),
                    "entreprise": html.unescape(j.get("companyName") or ""),
                    "categorie": categorie,
                    "zone": j.get("jobGeo"),
                    "contrat": contrat_jobicy(j.get("jobType")),
                    "salaire": sum(valeurs) / len(valeurs) if en_euros and valeurs else None,
                    "teletravail": True,
                    "date": (j.get("pubDate") or "")[:10],
                    "url": j.get("url"),
                }
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            erreurs.append(f"{categorie} : {e}")
    if erreurs:
        print("Jobicy, requêtes en échec :", " ; ".join(erreurs))
    if not offres:
        return None
    DOSSIER_JOBICY.mkdir(parents=True, exist_ok=True)
    fichier = DOSSIER_JOBICY / f"{jour}.json"
    fichier.write_text(json.dumps(sorted(offres.values(), key=lambda o: o["id"]), ensure_ascii=False, indent=1),
                       encoding="utf-8")
    return fichier


def offres_jobicy():
    """Dernière collecte Jobicy enregistrée (l'API renvoie les offres des 7 derniers jours)."""
    fichiers = sorted(DOSSIER_JOBICY.glob("*.json")) if DOSSIER_JOBICY.exists() else []
    if not fichiers:
        return None, []
    return fichiers[-1].stem, json.loads(fichiers[-1].read_text(encoding="utf-8"))


# ----------------------------------------------------------------------------- agrégats
def agreger(offres, contrats_publies=True):
    salaires = [o["salaire"] for o in offres if o.get("salaire")]
    contrats = Counter(o["contrat"] for o in offres)
    nb_tele = sum(1 for o in offres if o.get("teletravail"))

    groupes = defaultdict(list)
    for o in offres:
        if o["intitule"]:
            groupes[(o["intitule"].lower(), o["source"])].append(o)
    top = sorted(groupes.values(), key=len, reverse=True)[:TOP_N]

    return {
        "nb_offres": len(offres),
        "salaires": {
            "min": round(min(salaires)) if salaires else None,
            "max": round(max(salaires)) if salaires else None,
            "mediane": round(statistics.median(salaires)) if salaires else None,
            "moyenne": round(statistics.fmean(salaires)) if salaires else None,
            "nb_renseignes": len(salaires),
        },
        "contrats": {c: contrats.get(c, 0) for c in CONTRATS},
        "contrats_publies": contrats_publies,
        "teletravail": {"nb": nb_tele, "part": round(nb_tele / len(offres), 4) if offres else None},
        "top_postes": [{
            "intitule": g[0]["intitule"],
            "nb": len(g),
            "salaire_median": round(statistics.median(s)) if (s := [o["salaire"] for o in g if o.get("salaire")]) else None,
            "source": g[0]["source"],
            "site": Counter(o["site"] for o in g).most_common(1)[0][0],
        } for g in top],
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sans-jobicy", action="store_true", help="ne pas interroger Jobicy")
    args = ap.parse_args(argv)

    jour, ft = offres_france_travail()
    if not args.sans_jobicy:
        fichier = collecter_jobicy(f"{date.today():%Y-%m-%d}")
        print(f"Jobicy : {'collecte enregistrée dans ' + str(fichier.relative_to(RACINE)) if fichier else 'aucune offre récupérée, on garde la dernière collecte'}")
    jour_jobicy, jobicy = offres_jobicy()
    jobicy = [o for o in jobicy if JOBICY_INTITULES.search(o["intitule"] or "")]

    directes = [o for o in ft if o["source"] == "france_travail"]
    partenaires = [o for o in ft if o["source"] == "partenaires"]
    sites = Counter(o["site"] for o in partenaires)

    sources = {
        "france_travail": {
            **agreger(directes),
            "libelle": "France Travail",
            "description": "Offres déposées directement sur francetravail.fr",
        },
        "partenaires": {
            **agreger(partenaires),
            "libelle": "Sites partenaires",
            "description": "Offres d'autres sites d'emploi rediffusées par l'API France Travail",
            "sites": [{"nom": n, "nb": c} for n, c in sites.most_common(12)],
        },
    }
    if jobicy:
        sources["jobicy"] = {
            **agreger(jobicy, contrats_publies=False),
            "libelle": "Jobicy",
            "description": "Offres en télétravail ouvertes depuis la France : marketing, contenu, communication, design",
            "date_collecte": jour_jobicy,
            "credit": {"texte": "Offres en télétravail fournies par Jobicy", "url": "https://jobicy.com"},
        }

    toutes = directes + partenaires + jobicy
    resume = {
        "date_maj": jour,
        "perimetre": "Métiers du marketing et du digital : 23 codes ROME pour France Travail et ses "
                     "partenaires ; pour Jobicy, offres en télétravail ouvertes depuis la France dont "
                     "l'intitulé relève du marketing, du contenu, de la communication ou du design.",
        "global": agreger(toutes, contrats_publies=True),
        "sources": sources,
    }
    SORTIE.parent.mkdir(parents=True, exist_ok=True)
    tmp = SORTIE.with_suffix(".tmp")
    tmp.write_text(json.dumps(resume, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(SORTIE)

    print(f"Écrit : {SORTIE.relative_to(RACINE)} — {len(toutes)} offres : France Travail {len(directes)}, "
          f"sites partenaires {len(partenaires)} ({', '.join(f'{n} {c}' for n, c in sites.most_common(4))}…), "
          f"Jobicy {len(jobicy)}")


if __name__ == "__main__":
    main()
