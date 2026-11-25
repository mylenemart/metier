r"""Écrit data/observatoire.json : la synthèse par source qu'affiche observatoire.html.

La synthèse est cumulative : elle porte sur toutes les offres vues depuis le début de la veille
(data/brut et data/jobicy gardent tout l'historique) ; chaque jour, les nouvelles offres s'ajoutent
aux anciennes, qui restent comptées même après leur retrait du site d'origine.

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
import math
import os
import re
import statistics
import sys
import time
import urllib.error
import urllib.parse
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
    """Toutes les offres vues depuis le début, chacune dans sa dernière version connue,
    avec la date à laquelle on l'a vue pour la première fois et si elle est encore en ligne."""
    jours = sorted(f for f in (RACINE / "data" / "actives").glob("*.csv")
                   if re.fullmatch(r"\d{4}-\d{2}-\d{2}", f.stem))
    if not jours:
        raise SystemExit("Aucune extraction France Travail : lancez d'abord scripts/extraire.py")
    with jours[-1].open(encoding="utf-8") as f:
        ids_actifs = {r["id"] for r in csv.DictReader(f)}

    versions, premiere = {}, {}
    for fichier in sorted((RACINE / "data" / "brut").glob("*/*.jsonl")):
        with fichier.open(encoding="utf-8") as fh:
            for ligne in fh:
                if ligne.strip():
                    v = json.loads(ligne)
                    oid = v["id"]
                    premiere[oid] = min(premiere.get(oid, v["vu_le"]), v["vu_le"])
                    if oid not in versions or v["vu_le"] >= versions[oid]["vu_le"]:
                        versions[oid] = v

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
            "entreprise": (o.get("entreprise") or {}).get("nom") or "",
            "contrat": contrat_france_travail(o),
            "salaire": salaire_annuel(o),
            "teletravail": teletravail(texte),
            "premiere_vue": premiere[oid],
            "active": oid in ids_actifs,
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
    return enregistrer("jobicy", jour, offres)


# ----------------------------------------------------------------------------- autres sites en accès libre
# Trois API publiques, gratuites et sans clé. Leurs conditions : citer le site avec un lien et
# renvoyer vers l'annonce d'origine (champ « url ») ; une collecte par jour reste très en deçà des limites.
#   Himalayas  https://himalayas.app/api      offres en télétravail ouvertes aux candidats en France
#   Arbeitnow  https://www.arbeitnow.com/blog/job-board-api   offres d'entreprises européennes (ATS)
#   Remotive   https://remotive.com/api-documentation         offres marketing en télétravail
SITES_LIBRES = {
    "himalayas": {"libelle": "Himalayas", "url": "https://himalayas.app",
                  "description": "Offres en télétravail ouvertes aux candidats basés en France"},
    "arbeitnow": {"libelle": "Arbeitnow", "url": "https://www.arbeitnow.com",
                  "description": "Offres d'entreprises européennes situées en France"},
    "remotive": {"libelle": "Remotive", "url": "https://remotive.com",
                 "description": "Offres marketing en télétravail ouvertes à la France ou à l'Europe"},
    # Agrégateurs français : API officielles gratuites, avec une clé personnelle (secrets GitHub).
    # Sans clé, la source est simplement ignorée.
    "adzuna": {"libelle": "Adzuna", "url": "https://www.adzuna.fr",
               "description": "Agrégateur d'offres françaises (nombreux sites d'emploi), via son API officielle",
               "contrats": True},
    "jooble": {"libelle": "Jooble", "url": "https://fr.jooble.org",
               "description": "Agrégateur d'offres françaises, via son API officielle",
               "contrats": True},
}
RECHERCHES_FR = {"marketing": 10, "communication": 3, "community manager": 2, "seo": 2,
                 "e-commerce": 2, "chef de produit": 2, "marketing digital": 3}
HIMALAYAS_RECHERCHES = {"marketing": 5, "seo": 1, "content": 1, "social media": 1, "e-commerce": 1}
ZONES_FRANCE = re.compile(r"france|europe|emea|worldwide|anywhere|partout|paris", re.IGNORECASE)
PERIODES = {"year": 1, "annual": 1, "yearly": 1, "month": 12, "monthly": 12, "hour": 1607, "hourly": 1607}


def lire_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def enregistrer(slug, jour, offres):
    """Écrit la collecte du jour dans data/<slug>/<jour>.json (rien si la collecte est vide)."""
    if not offres:
        return None
    dossier = RACINE / "data" / slug
    dossier.mkdir(parents=True, exist_ok=True)
    fichier = dossier / f"{jour}.json"
    fichier.write_text(json.dumps(sorted(offres.values(), key=lambda o: o["id"]), ensure_ascii=False, indent=1),
                       encoding="utf-8")
    return fichier


def salaire_euros(minimum, maximum, devise, periode="year"):
    """Salaire annuel brut en euros, ou None si la devise n'est pas l'euro."""
    if (devise or "").upper() not in ("EUR", "€"):
        return None
    valeurs = [float(x) for x in (minimum, maximum) if isinstance(x, (int, float)) and x > 0]
    if not valeurs:
        return None
    annuel = sum(valeurs) / len(valeurs) * PERIODES.get((periode or "year").lower(), 1)
    return annuel if 4000 <= annuel <= 250000 else None


def contrat_texte(texte):
    t = (texte or "").lower()
    if re.search(r"intern|stage|praktik|alternan|apprenti|werkstudent|working student", t):
        return "Alternance/Stage"
    if re.search(r"\bcdi\b|permanent|festanstellung|unbefristet|durée indéterminée", t):
        return "CDI"
    if re.search(r"\bcdd\b|temporary|befristet|fixed[- ]term|durée déterminée|int[ée]rim", t):
        return "CDD"
    return "Autre"


def offre_libre(slug, oid, intitule, entreprise, contrat, salaire, teletravail_, date_pub, url, zone=None):
    return {"id": f"{slug}-{oid}", "source": slug, "site": SITES_LIBRES[slug]["libelle"],
            "intitule": nettoyer_intitule(intitule), "entreprise": html.unescape(entreprise or ""),
            "zone": zone, "contrat": contrat, "salaire": salaire, "teletravail": teletravail_,
            "date": (date_pub or "")[:10], "url": url}


def collecter_himalayas():
    offres = {}
    for q, pages in HIMALAYAS_RECHERCHES.items():
        for page in range(1, pages + 1):
            time.sleep(3)  # sans clé, l'API limite le nombre d'appels par minute
            try:
                d = lire_json(f"https://himalayas.app/jobs/api/search?q={urllib.parse.quote(q)}&country=France&page={page}")
            except urllib.error.HTTPError as e:
                if e.code == 429:  # limite atteinte : on garde ce qui a déjà été reçu
                    print(f"Himalayas : limite d'appels atteinte, {len(offres)} offres gardées")
                    return offres
                raise
            jobs = d.get("jobs") or []
            for j in jobs:
                oid = j.get("guid") or j.get("applicationLink") or j.get("title")
                pub = j.get("pubDate")
                if isinstance(pub, (int, float)):
                    pub = date.fromtimestamp(pub).isoformat()
                offres[f"himalayas-{oid}"] = offre_libre(
                    "himalayas", oid, j.get("title"), j.get("companyName"), contrat_texte(j.get("employmentType")),
                    salaire_euros(j.get("minSalary"), j.get("maxSalary"), j.get("currency"), j.get("salaryPeriod")),
                    True, str(pub or ""), j.get("applicationLink"), ", ".join(j.get("locationRestrictions") or []) or "Monde")
            if len(jobs) < 20:
                break
    return offres


def collecter_arbeitnow(pages=10):
    offres = {}
    for page in range(1, pages + 1):
        d = lire_json(f"https://www.arbeitnow.com/api/job-board-api?page={page}")
        for j in d.get("data") or []:
            if "france" not in (j.get("location") or "").lower():
                continue
            cree = j.get("created_at")
            offres[f"arbeitnow-{j.get('slug')}"] = offre_libre(
                "arbeitnow", j.get("slug"), j.get("title"), j.get("company_name"),
                contrat_texte(" ".join(j.get("job_types") or [])), None, bool(j.get("remote")),
                date.fromtimestamp(cree).isoformat() if isinstance(cree, (int, float)) else "", j.get("url"),
                j.get("location"))
        if not (d.get("links") or {}).get("next"):
            break
        time.sleep(1)
    return offres


def salaire_remotive(texte):
    """« €40k - €50k », « 45,000 - 55,000 EUR » -> moyenne annuelle en euros ; autres devises ignorées."""
    t = (texte or "").lower().replace(",", "").replace(" ", "")
    if "€" not in t and "eur" not in t:
        return None
    valeurs = [float(n) * (1000 if k else 1) for n, k in re.findall(r"(\d+(?:\.\d+)?)(k?)", t)]
    return salaire_euros(min(valeurs), max(valeurs), "EUR") if valeurs else None


def collecter_remotive():
    offres = {}
    for j in lire_json("https://remotive.com/api/remote-jobs?category=marketing").get("jobs") or []:
        zone = j.get("candidate_required_location") or ""
        if not ZONES_FRANCE.search(zone):
            continue
        offres[f"remotive-{j.get('id')}"] = offre_libre(
            "remotive", j.get("id"), j.get("title"), j.get("company_name"), contrat_texte(j.get("job_type")),
            salaire_remotive(j.get("salary")), True, j.get("publication_date"), j.get("url"), zone)
    return offres


def collecter_adzuna():
    """API Adzuna (https://developer.adzuna.com) : 50 offres par page, offres des 2 derniers jours."""
    app_id, app_key = os.getenv("ADZUNA_APP_ID"), os.getenv("ADZUNA_APP_KEY")
    if not app_id or not app_key:
        print("Adzuna : pas de clé (secrets ADZUNA_APP_ID et ADZUNA_APP_KEY), source ignorée")
        return {}
    contrats = {"permanent": "CDI", "contract": "CDD"}
    offres = {}
    for q, pages in RECHERCHES_FR.items():
        for page in range(1, pages + 1):
            time.sleep(1)
            d = lire_json(f"https://api.adzuna.com/v1/api/jobs/fr/search/{page}?app_id={app_id}&app_key={app_key}"
                          f"&results_per_page=50&max_days_old=2&what={urllib.parse.quote(q)}&content-type=application/json")
            res = d.get("results") or []
            for j in res:
                predit = str(j.get("salary_is_predicted")) == "1"  # salaire estimé par Adzuna : écarté
                offres[f"adzuna-{j.get('id')}"] = offre_libre(
                    "adzuna", j.get("id"), j.get("title"), (j.get("company") or {}).get("display_name"),
                    contrats.get(j.get("contract_type")) or contrat_texte(j.get("title")),
                    None if predit else salaire_euros(j.get("salary_min"), j.get("salary_max"), "EUR"),
                    teletravail(f"{j.get('title') or ''} {j.get('description') or ''}"),
                    j.get("created"), j.get("redirect_url"), (j.get("location") or {}).get("display_name"))
            if len(res) < 50:
                break
    return offres


def collecter_jooble():
    """API Jooble (https://jooble.org/api/about) : requêtes POST, 20 offres par page."""
    cle = os.getenv("JOOBLE_KEY")
    if not cle:
        print("Jooble : pas de clé (secret JOOBLE_KEY), source ignorée")
        return {}
    offres = {}
    for q, pages in RECHERCHES_FR.items():
        for page in range(1, min(pages, 5) + 1):
            time.sleep(1)
            corps = json.dumps({"keywords": q, "location": "France", "page": page}).encode()
            req = urllib.request.Request(f"https://jooble.org/api/{cle}", data=corps,
                                         headers={"Content-Type": "application/json", "User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as r:
                jobs = json.loads(r.read().decode("utf-8")).get("jobs") or []
            for j in jobs:
                offres[f"jooble-{j.get('id')}"] = offre_libre(
                    "jooble", j.get("id"), j.get("title"), j.get("company"),
                    contrat_texte(f"{j.get('type') or ''} {j.get('title') or ''}"), salaire_remotive(j.get("salary")),
                    teletravail(f"{j.get('title') or ''} {j.get('snippet') or ''}"),
                    j.get("updated"), j.get("link"), j.get("location"))
            if len(jobs) < 20:
                break
    return offres


COLLECTEURS = {"himalayas": collecter_himalayas, "arbeitnow": collecter_arbeitnow, "remotive": collecter_remotive,
               "adzuna": collecter_adzuna, "jooble": collecter_jooble}


def offres_collectees(slug):
    """Toutes les offres d'un site collectées jour après jour : chaque collecte s'ajoute aux précédentes."""
    dossier = RACINE / "data" / slug
    fichiers = sorted(dossier.glob("*.json")) if dossier.exists() else []
    if not fichiers:
        return None, []
    offres = {}
    for fichier in fichiers:
        for o in json.loads(fichier.read_text(encoding="utf-8")):
            premiere = offres[o["id"]]["premiere_vue"] if o["id"] in offres else fichier.stem
            offres[o["id"]] = {**o, "premiere_vue": premiere, "active": False}
    for o in json.loads(fichiers[-1].read_text(encoding="utf-8")):
        offres[o["id"]]["active"] = True
    return fichiers[-1].stem, list(offres.values())


def offres_jobicy():
    return offres_collectees("jobicy")



# ----------------------------------------------------------------------------- statistiques
# Calculs faits à la main (bibliothèque standard) pour que l'Action GitHub n'ait rien à installer.
TRANCHES = list(range(0, 100001, 5000))  # histogramme des salaires : tranches de 5 000 euros


def quantile(valeurs, q):
    v = sorted(valeurs)
    if not v:
        return None
    pos = (len(v) - 1) * q
    bas = int(pos)
    return v[bas] + (v[min(bas + 1, len(v) - 1)] - v[bas]) * (pos - bas)


def forme(valeurs):
    """Asymétrie et aplatissement « en excès » (0 pour une loi normale), calculés sur les moments."""
    n = len(valeurs)
    if n < 3:
        return None, None
    m = sum(valeurs) / n
    m2 = sum((x - m) ** 2 for x in valeurs) / n
    m3 = sum((x - m) ** 3 for x in valeurs) / n
    m4 = sum((x - m) ** 4 for x in valeurs) / n
    return (round(m3 / m2 ** 1.5, 2), round(m4 / m2 ** 2 - 3, 2)) if m2 else (None, None)


def histogramme(valeurs):
    comptes = [0] * len(TRANCHES)
    for x in valeurs:
        comptes[min(int(x // 5000), len(TRANCHES) - 1)] += 1
    return comptes


def _gamma_q(a, x):
    """Fonction gamma incomplète régularisée Q(a, x) (Numerical Recipes, § 6.2)."""
    if x <= 0:
        return 1.0
    lg = math.lgamma(a)
    if x < a + 1:  # série
        terme = somme = 1.0 / a
        ap = a
        for _ in range(1000):
            ap += 1
            terme *= x / ap
            somme += terme
            if abs(terme) < abs(somme) * 1e-15:
                break
        return 1.0 - somme * math.exp(-x + a * math.log(x) - lg)
    b, c, d = x + 1 - a, 1e300, 1 / (x + 1 - a)  # fraction continue
    h = d
    for i in range(1, 1000):
        an = -i * (i - a)
        b += 2
        d = an * d + b
        d = 1e-300 if abs(d) < 1e-300 else d
        c = b + an / c
        c = 1e-300 if abs(c) < 1e-300 else c
        d = 1 / d
        h *= d * c
        if abs(d * c - 1) < 1e-15:
            break
    return math.exp(-x + a * math.log(x) - lg) * h


def _beta_i(a, b, x):
    """Fonction bêta incomplète régularisée I_x(a, b) (Numerical Recipes, § 6.4)."""
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    bt = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log(1 - x))
    if x >= (a + 1) / (a + b + 2):
        return 1 - _beta_i(b, a, 1 - x)
    qab, qap, qam = a + b, a + 1, a - 1
    c, d = 1.0, 1 - qab * x / qap
    d = 1 / (1e-300 if abs(d) < 1e-300 else d)
    h = d
    for m in range(1, 1000):
        m2 = 2 * m
        for aa in (m * (b - m) * x / ((qam + m2) * (a + m2)), -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))):
            d = 1 + aa * d
            d = 1 / (1e-300 if abs(d) < 1e-300 else d)
            c = 1 + aa / c
            c = 1e-300 if abs(c) < 1e-300 else c
            h *= d * c
        if abs(d * c - 1) < 1e-15:
            break
    return bt * h / a


def p_chi2(stat, ddl):
    return _gamma_q(ddl / 2, stat / 2)


def p_fisher(f, d1, d2):
    return _beta_i(d2 / 2, d1 / 2, d2 / (d2 + d1 * f)) if f > 0 else 1.0


def fr(x, decimales=1):
    return f"{x:.{decimales}f}".replace(".", ",")


def taille_effet(valeur, seuils):
    petit, moyen, fort = seuils
    return "fort" if valeur >= fort else "moyen" if valeur >= moyen else "petit" if valeur >= petit else "négligeable"


def test_chi2(offres, ligne, colonne, nom, lignes_ok=None):
    """Chi² d'indépendance entre deux variables qualitatives, avec le V de Cramer."""
    table = defaultdict(Counter)
    for o in offres:
        l, c = ligne(o), colonne(o)
        if l is not None and c is not None and (lignes_ok is None or l in lignes_ok):
            table[l][c] += 1
    lignes = sorted(table)
    colonnes = sorted({c for r in table.values() for c in r})
    n = sum(sum(r.values()) for r in table.values())
    if len(lignes) < 2 or len(colonnes) < 2 or not n:
        return None
    tot_l = {l: sum(table[l].values()) for l in lignes}
    tot_c = {c: sum(table[l][c] for l in lignes) for c in colonnes}
    stat = sum((table[l][c] - tot_l[l] * tot_c[c] / n) ** 2 / (tot_l[l] * tot_c[c] / n) for l in lignes for c in colonnes)
    ddl = (len(lignes) - 1) * (len(colonnes) - 1)
    k = min(len(lignes), len(colonnes)) - 1
    v = math.sqrt(stat / (n * k))
    attendus_faibles = sum(1 for l in lignes for c in colonnes if tot_l[l] * tot_c[c] / n < 5)
    return {"test": "Chi² d'indépendance", "variables": nom, "n": n, "statistique": f"χ² = {fr(stat)}",
            "ddl": ddl, "p": p_chi2(stat, ddl), "effet": f"V de Cramer = {fr(v, 2)}",
            "effet_valeur": round(v, 3), "effet_niveau": taille_effet(v, (0.1, 0.3, 0.5) if k == 1 else (0.07, 0.21, 0.35)),
            "condition": "effectifs attendus ≥ 5 dans toutes les cases" if not attendus_faibles
                         else f"{attendus_faibles} case(s) avec un effectif attendu < 5 : résultat à prendre avec prudence",
            "tableau": {"lignes": lignes, "colonnes": colonnes,
                        "valeurs": [[table[l][c] for c in colonnes] for l in lignes]}}


def test_anova(groupes, nom):
    """ANOVA à un facteur (êta²) et, parce que les salaires sont asymétriques, Kruskal-Wallis (epsilon²)."""
    groupes = {k: v for k, v in groupes.items() if len(v) >= 2}
    if len(groupes) < 2:
        return None
    toutes = [x for v in groupes.values() for x in v]
    n, k = len(toutes), len(groupes)
    m = sum(toutes) / n
    sce = sum(len(v) * (sum(v) / len(v) - m) ** 2 for v in groupes.values())
    sct = sum((x - m) ** 2 for x in toutes)
    scr = sct - sce
    f = (sce / (k - 1)) / (scr / (n - k)) if scr else float("inf")
    eta2 = sce / sct if sct else 0
    # Kruskal-Wallis sur les rangs (rangs moyens en cas d'ex aequo)
    tri = sorted((x, g) for g, v in groupes.items() for x in v)
    rangs, i = defaultdict(list), 0
    while i < n:
        j = i
        while j + 1 < n and tri[j + 1][0] == tri[i][0]:
            j += 1
        for t in range(i, j + 1):
            rangs[tri[t][1]].append((i + j) / 2 + 1)
        i = j + 1
    h = 12 / (n * (n + 1)) * sum(sum(r) ** 2 / len(r) for r in rangs.values()) - 3 * (n + 1)
    eps2 = h / (n - 1)
    effectifs = ", ".join(f"{g} : {len(v)}" for g, v in groupes.items())
    return [
        {"test": "ANOVA à un facteur", "variables": nom, "n": n, "statistique": f"F({k - 1} ; {n - k}) = {fr(f)}",
         "ddl": f"{k - 1} ; {n - k}", "p": p_fisher(f, k - 1, n - k), "effet": f"êta² = {fr(eta2, 3)}",
         "effet_valeur": round(eta2, 4), "effet_niveau": taille_effet(eta2, (0.01, 0.06, 0.14)),
         "condition": f"suppose des salaires proches de la loi normale dans chaque groupe ({effectifs}) : "
                      "ce n'est pas le cas (asymétrie positive), d'où le test de Kruskal-Wallis en contrôle"},
        {"test": "Kruskal-Wallis", "variables": nom, "n": n, "statistique": f"H = {fr(h)}", "ddl": k - 1,
         "p": p_chi2(h, k - 1), "effet": f"epsilon² = {fr(eps2, 3)}", "effet_valeur": round(eps2, 4),
         "effet_niveau": taille_effet(eps2, (0.01, 0.06, 0.14)),
         "condition": "compare les rangs : ne suppose pas la normalité, adapté à des salaires asymétriques"},
    ]

# ----------------------------------------------------------------------------- agrégats
def agreger(offres, contrats_publies=True, jour=None):
    jour = jour or max((o["premiere_vue"] for o in offres), default=None)
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
        "nouvelles": sum(1 for o in offres if o["premiere_vue"] == jour),
        "actives": sum(1 for o in offres if o.get("active")),
        "jour_nouvelles": jour,
        "salaires": {
            "min": round(min(salaires)) if salaires else None,
            "max": round(max(salaires)) if salaires else None,
            "mediane": round(statistics.median(salaires)) if salaires else None,
            "moyenne": round(statistics.fmean(salaires)) if salaires else None,
            "nb_renseignes": len(salaires),
            "q1": round(quantile(salaires, 0.25)) if salaires else None,
            "q3": round(quantile(salaires, 0.75)) if salaires else None,
            "ecart_type": round(statistics.stdev(salaires)) if len(salaires) > 1 else None,
            "asymetrie": forme(salaires)[0],
            "aplatissement": forme(salaires)[1],
            "histogramme": histogramme(salaires),
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
    ap.add_argument("--sans-jobicy", action="store_true", help="ne rien collecter (Jobicy et autres sites)")
    args = ap.parse_args(argv)

    jour, ft = offres_france_travail()
    if not args.sans_jobicy:
        fichier = collecter_jobicy(f"{date.today():%Y-%m-%d}")
        print(f"Jobicy : {'collecte enregistrée dans ' + str(fichier.relative_to(RACINE)) if fichier else 'aucune offre récupérée, on garde la dernière collecte'}")
        aujourdhui = f"{date.today():%Y-%m-%d}"
        for slug, collecter in COLLECTEURS.items():
            try:
                fichier = enregistrer(slug, aujourdhui, collecter())
                print(f"{SITES_LIBRES[slug]['libelle']} : {'collecte enregistrée' if fichier else 'aucune offre'}")
            except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as e:
                print(f"{SITES_LIBRES[slug]['libelle']} : collecte impossible ({e}), on garde les précédentes")
    jour_jobicy, jobicy = offres_jobicy()
    jobicy = [o for o in jobicy if JOBICY_INTITULES.search(o["intitule"] or "")]

    directes = [o for o in ft if o["source"] == "france_travail"]
    partenaires = [o for o in ft if o["source"] == "partenaires"]
    sites = Counter(o["site"] for o in partenaires)

    sources = {
        "france_travail": {
            **agreger(directes, jour=jour),
            "libelle": "France Travail",
            "description": "Offres déposées directement sur francetravail.fr",
        },
        "partenaires": {
            **agreger(partenaires, jour=jour),
            "libelle": "Sites partenaires",
            "description": "Offres d'autres sites d'emploi rediffusées par l'API France Travail",
            "sites": [{"nom": n, "nb": c} for n, c in sites.most_common(12)],
        },
    }
    if jobicy:
        sources["jobicy"] = {
            **agreger(jobicy, contrats_publies=False, jour=jour_jobicy),
            "libelle": "Jobicy",
            "description": "Offres en télétravail ouvertes depuis la France : marketing, contenu, communication, design",
            "date_collecte": jour_jobicy,
            "credit": {"texte": "Offres en télétravail fournies par Jobicy", "url": "https://jobicy.com"},
        }

    libres, jours_collecte = [], [jour, jour_jobicy]
    for slug, info in SITES_LIBRES.items():
        jour_site, offres = offres_collectees(slug)
        offres = [o for o in offres if JOBICY_INTITULES.search(o["intitule"] or "")]
        if not offres:
            continue
        sources[slug] = {
            **agreger(offres, contrats_publies=info.get("contrats", False), jour=jour_site),
            "libelle": info["libelle"], "description": info["description"],
            "date_collecte": jour_site,
            "credit": {"texte": f"Offres fournies par {info['libelle']}", "url": info["url"]},
        }
        libres += offres
        jours_collecte.append(jour_site)

    toutes = directes + partenaires + jobicy + libres
    debut = min(o["premiere_vue"] for o in toutes)
    par_jour = defaultdict(Counter)
    for o in toutes:
        par_jour[o["premiere_vue"]][o["source"]] += 1
    libelles = {k: v["libelle"] for k, v in sources.items()}
    tests = [t for t in [
        test_chi2(directes + partenaires, lambda o: libelles[o["source"]],
                  lambda o: o["contrat"] if o["contrat"] != "Autre" else None,
                  "Origine de l'offre × type de contrat (CDI, CDD, alternance/stage)"),
        test_chi2(toutes, lambda o: libelles[o["source"]], lambda o: "télétravail" if o.get("teletravail") else "sur site",
                  "Source × télétravail"),
    ] if t]
    salaires_par_source = {libelles[k]: [o["salaire"] for o in toutes if o["source"] == k and o.get("salaire")]
                           for k in sources}
    tests += test_anova({k: v for k, v in salaires_par_source.items() if len(v) >= 15},
                        "Source × salaire annuel brut (sources avec au moins 15 salaires)") or []

    # Doublons probables : même intitulé, même entreprise, même source (annonces publiées en série)
    cles = Counter((o["source"], (o["intitule"] or "").lower(), (o.get("entreprise") or "").lower())
                   for o in toutes if o.get("entreprise"))
    doublons = sum(c - 1 for c in cles.values() if c > 1)

    cumul, historique = 0, []
    for d, c in sorted(par_jour.items()):
        cumul += sum(c.values())
        historique.append({"date": d, "nouvelles": dict(c), "cumul": cumul})

    resume = {
        "date_maj": max(j for j in jours_collecte if j),
        "date_france_travail": jour,
        "depuis": debut,
        "historique": historique,
        "tests": tests,
        "doublons_probables": doublons,
        "tranches_salaire": TRANCHES,
        "perimetre": f"Toutes les offres vues depuis le {debut} (les offres retirées restent comptées). "
                     "Métiers du marketing et du digital : 23 codes ROME pour France Travail et ses "
                     "partenaires ; pour Adzuna et Jooble, recherches par mots-clés en France ; pour Jobicy, Himalayas, Arbeitnow et Remotive, offres ouvertes aux candidats "
                     "en France dont l'intitulé relève du marketing, du contenu, de la communication ou du design.",
        "global": {**agreger(toutes, contrats_publies=True, jour=jour),
                   "nouvelles": sum(1 for o in toutes if o["premiere_vue"] in jours_collecte)},
        "sources": sources,
    }
    SORTIE.parent.mkdir(parents=True, exist_ok=True)
    tmp = SORTIE.with_suffix(".tmp")
    tmp.write_text(json.dumps(resume, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(SORTIE)

    print(f"Écrit : {SORTIE.relative_to(RACINE)} — {len(toutes)} offres depuis le {debut} "
          f"(+{resume['global']['nouvelles']} nouvelles) : France Travail {len(directes)}, "
          f"sites partenaires {len(partenaires)} ({', '.join(f'{n} {c}' for n, c in sites.most_common(4))}…), "
          f"Jobicy {len(jobicy)}, autres sites {len(libres)}")


if __name__ == "__main__":
    main()
