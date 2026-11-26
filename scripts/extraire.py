r"""Récupère les offres France Travail des métiers suivis et les enregistre dans data/.

Usage :
    .venv\Scripts\python.exe scripts\extraire.py                 # tous les métiers de METIERS
    .venv\Scripts\python.exe scripts\extraire.py --verifier      # teste seulement la connexion
    .venv\Scripts\python.exe scripts\extraire.py --rome M1718    # un seul code, pour essayer

Ce que ça écrit :
    data/brut/<AAAA-MM>/<ROME>.jsonl   une ligne par offre complète (JSON tel que l'API le renvoie),
                                       écrite la première fois qu'on voit l'offre, et de nouveau
                                       si son contenu a changé (une version = une ligne, datée)
    data/actives/<date>.csv            les offres actives ce jour-là : rome, id, date d'actualisation
    data/serie.csv                     une ligne par métier et par jour : total, nouvelles, modifiées

Les identifiants sont lus dans le fichier .env (voir .env.example) ou dans l'environnement
(secrets GitHub Actions). API : https://francetravail.io/data/api/offres-emploi —
150 offres par appel, 1 150 par requête, total réel dans l'en-tête Content-Range.
"""
import argparse
import csv
import hashlib
import json
import os
import re
import sys
import time
from datetime import date
from pathlib import Path

import requests
from dotenv import load_dotenv

from zone import ZONES

RACINE = Path(__file__).resolve().parent.parent
load_dotenv(RACINE / ".env")

# Les métiers suivis : code ROME -> (libellé, groupe, coché par défaut sur la page).
METIERS = {
    # Marketing et stratégie
    "M1707": ("Responsable de la stratégie commerciale", "Marketing", True),
    "M1718": ("Chargé(e) de marketing digital", "Marketing", True),
    # Digital et e-commerce
    "E1113": ("Responsable e-commerce", "Digital", True),
    "D1438": ("Assistant(e) e-commerce", "Digital", True),
    "E1124": ("Social media manager", "Digital", True),
    "M1426": ("Chief digital officer", "Digital", True),
    # Commerce, relation client et merchandising
    "D1415": ("Chargé(e) de relation client (CRM)", "Commerce", True),
    "D1302": ("Responsable de boutique", "Commerce", True),
    "D1510": ("Chef(fe) de secteur magasin", "Commerce", True),
    "D1516": ("Responsable merchandising", "Commerce", True),
    "D1506": ("Chargé(e) de merchandising", "Commerce", True),
}

TOKEN_URL = "https://entreprise.francetravail.fr/connexion/oauth2/access_token?realm=/partenaire"
SEARCH_URL = "https://api.francetravail.io/partenaire/offresdemploi/v2/offres/search"

# Champs qui bougent sans que l'offre change : ignorés pour décider si une offre a été modifiée.
CHAMPS_VOLATILS = {"dateActualisation"}


def obtenir_token():
    # Les identifiants ne contiennent jamais d'espace : on retire ceux d'un copier-coller
    cid, secret = (re.sub(r"\s+", "", os.getenv(k) or "") for k in ("FT_CLIENT_ID", "FT_CLIENT_SECRET"))
    if not cid or not secret or cid.startswith("PAR_votre"):
        sys.exit("Identifiants absents : copiez .env.example en .env et remplissez-le.")
    r = requests.post(TOKEN_URL, data={
        "grant_type": "client_credentials",
        "client_id": cid,
        "client_secret": secret,
        "scope": "api_offresdemploiv2 o2dsoffre",
    }, headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=30)
    if r.status_code != 200:
        # France Travail explique le refus : invalid_client (identifiant ou secret faux),
        # invalid_scope (application non abonnée à l'API Offres d'emploi v2)…
        indice = (f"identifiant de {len(cid)} caractères, {'commence bien' if cid.startswith('PAR_') else 'ne commence pas'} "
                  f"par PAR_ ; secret de {len(secret)} caractères")
        sys.exit(f"Connexion refusée par France Travail ({r.status_code}) : {r.text[:300]}\n({indice})")
    return r.json()["access_token"]


def chercher(token, params, pas=150, maximum=1150):
    """Pagine la recherche ; renvoie (liste d'offres, total annoncé par l'API dans Content-Range)."""
    offres, total, debut = [], None, 0
    while debut < maximum:
        fin = min(debut + pas - 1, maximum - 1)
        for essai in range(6):  # 429 = trop de requêtes : on attend de plus en plus, puis on réessaie
            r = requests.get(SEARCH_URL, params=dict(params, range=f"{debut}-{fin}"),
                             headers={"Authorization": f"Bearer {token}"}, timeout=30)
            if r.status_code != 429:
                break
            attente = float(r.headers.get("Retry-After") or 0) or 2 ** (essai + 1)
            print(f"  France Travail demande de ralentir : pause de {attente:.0f} s")
            time.sleep(attente)
        if r.status_code == 204:                     # aucune offre
            break
        if r.status_code not in (200, 206):
            raise RuntimeError(f"{r.status_code} : {r.text[:200]}")
        m = re.search(r"/(\d+)", r.headers.get("Content-Range", ""))   # ex. "offres 0-149/1234"
        if m:
            total = int(m.group(1))
        lot = r.json().get("resultats", [])
        offres.extend(lot)
        if len(lot) < pas or (total is not None and len(offres) >= total):
            break
        debut += pas
        time.sleep(0.3)                              # on reste poli avec l'API
    return offres, total


def empreinte(offre):
    """Empreinte du contenu d'une offre, champs volatils exclus : change si l'annonce change."""
    stable = {k: v for k, v in offre.items() if k not in CHAMPS_VOLATILS}
    return hashlib.sha1(json.dumps(stable, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def versions_connues():
    """Toutes les (id, empreinte) déjà enregistrées dans data/brut, pour ne rien écrire deux fois."""
    vues = set()
    for f in (RACINE / "data" / "brut").glob("*/*.jsonl"):
        with f.open(encoding="utf-8") as fh:
            for ligne in fh:
                if ligne.strip():
                    v = json.loads(ligne)
                    vues.add((v["id"], v["empreinte"]))
    return vues


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--verifier", action="store_true", help="teste seulement la connexion")
    ap.add_argument("--rome", default="", help="un seul code ROME de METIERS, pour essayer")
    args = ap.parse_args()

    token = obtenir_token()
    print("Connexion à l'API France Travail : OK")
    if args.verifier:
        return
    codes = [args.rome] if args.rome else list(METIERS)
    if args.rome and args.rome not in METIERS:
        sys.exit(f"{args.rome} n'est pas dans METIERS (scripts/extraire.py).")

    aujourdhui = f"{date.today():%Y-%m-%d}"
    mois = aujourdhui[:7]
    vues = versions_connues()
    ids_connus = {i for i, _ in vues}
    (RACINE / "data" / "brut" / mois).mkdir(parents=True, exist_ok=True)
    (RACINE / "data" / "actives").mkdir(parents=True, exist_ok=True)

    actives, lignes_serie = [], []
    for code in codes:
        uniques, total = {}, 0
        for _, parametres, _ in ZONES.values():
            lot, annonce = chercher(token, dict(parametres, codeROME=code))
            uniques.update({o["id"]: o for o in lot})
            total += annonce if annonce is not None else len(lot)
            time.sleep(0.5)
        offres = list(uniques.values())
        nouvelles = modifiees = 0
        with (RACINE / "data" / "brut" / mois / f"{code}.jsonl").open("a", encoding="utf-8") as brut:
            for o in offres:
                e = empreinte(o)
                if (o["id"], e) not in vues:
                    if o["id"] in ids_connus:
                        modifiees += 1
                    else:
                        nouvelles += 1
                        ids_connus.add(o["id"])
                    vues.add((o["id"], e))
                    brut.write(json.dumps({"id": o["id"], "empreinte": e, "vu_le": aujourdhui,
                                           "rome": code, "offre": o}, ensure_ascii=False) + "\n")
                actives.append((code, o["id"], (o.get("dateActualisation") or "")[:10]))
        lignes_serie.append([aujourdhui, code, total if total is not None else len(offres),
                             len(offres), nouvelles, modifiees])
        print(f"{code}  {METIERS[code][0]:<48} {len(offres):5d} offres, {nouvelles:4d} nouvelles, {modifiees:3d} modifiées")
        time.sleep(1.5)  # on reste sous la limite de débit de l'API

    # Même logique pour les actives du jour : on remplace les codes relancés, on garde les autres.
    fichier_actives = RACINE / "data" / "actives" / f"{aujourdhui}.csv"
    if fichier_actives.exists():
        with fichier_actives.open(encoding="utf-8") as f:
            actives = [tuple(r) for r in list(csv.reader(f))[1:] if r[0] in METIERS and r[0] not in codes] + actives
    with fichier_actives.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["rome", "id", "date_actualisation"])
        w.writerows(sorted(actives))

    serie = RACINE / "data" / "serie.csv"
    lignes = []
    if serie.exists():
        with serie.open(encoding="utf-8") as f:
            lignes = [r for r in csv.reader(f)][1:]
    # Si on relance le même jour, la ligne du jour est remplacée, pas doublée.
    lignes = [r for r in lignes if not (r[0] == aujourdhui and (r[1] in codes or r[1] not in METIERS))] + lignes_serie
    with serie.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["date", "rome", "total", "recuperees", "nouvelles", "modifiees"])
        w.writerows(sorted(lignes))

    print(f"\n{aujourdhui} : {len(actives)} offres actives sur {len(codes)} métiers — "
          f"{sum(r[4] for r in lignes_serie)} nouvelles versions, {sum(r[5] for r in lignes_serie)} modifiées.")


if __name__ == "__main__":
    main()
