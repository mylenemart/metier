"""Collecte les offres en alternance de La bonne alternance, un code ROME à la fois.

API : GET /api/job/v1/search (https://api.apprentissage.beta.gouv.fr), clé lue dans LBA_API_KEY.
L'API renvoie au plus 150 offres par source et par recherche : la collecte n'est donc pas exhaustive,
mais les jours s'additionnent. Les entreprises « candidature spontanée » (recruiters) ne sont pas
des offres : elles sont ignorées. Les offres France Travail, déjà collectées par extraire.py, aussi.
"""
import json
import os
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

from extraire import METIERS

URL = "https://api.apprentissage.beta.gouv.fr/api/job/v1/search"


def cle_doublon(intitule, entreprise):
    """Intitulé + entreprise sans accents, casse, mention H/F ni ponctuation ; None sans entreprise."""
    def simple(t):
        t = unicodedata.normalize("NFD", (t or "").lower())
        t = "".join(c for c in t if not unicodedata.combining(c))
        t = re.sub(r"\b[hfx]\s*/\s*[hfx](\s*/\s*[hfx])?\b", " ", t)
        return " ".join(re.findall(r"[a-z0-9]+", t))
    i, e = simple(intitule), simple(entreprise)
    return (i, e) if i and e else None


def chercher(cle, rome):
    req = urllib.request.Request(f"{URL}?{urllib.parse.urlencode({'romes': rome})}",
                                 headers={"Authorization": f"Bearer {cle}", "Accept": "application/json",
                                          "User-Agent": "observatoire-emploi-etudiant (github.com/mylenemart/metier)"})
    for essai in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode("utf-8")).get("jobs") or []
        except urllib.error.HTTPError as e:
            if e.code != 429 or essai == 2:
                raise
            time.sleep(60)  # limite : 60 appels par minute


def collecter(deja_vues=()):
    """Renvoie {id: offre brute simplifiée}. deja_vues : clés (intitulé, entreprise) de France Travail."""
    cle = re.sub(r"\s+", "", os.getenv("LBA_API_KEY") or "")
    if not cle:
        raise ValueError("LBA_API_KEY absente")
    offres, doublons, deja_vues = {}, set(), set(deja_vues)
    for rome in METIERS:
        for j in chercher(cle, rome):
            ident, offre = j.get("identifier") or {}, j.get("offer") or {}
            partenaire = ident.get("partner_label") or ""
            oid = ident.get("id") or f"{partenaire}-{ident.get('partner_job_id')}"
            entreprise = (j.get("workplace") or {}).get("name") or (j.get("workplace") or {}).get("brand") or ""
            if re.search(r"france[\s_-]*travail", partenaire, re.IGNORECASE) or offre.get("status", "Active") != "Active":
                continue
            if cle_doublon(offre.get("title"), entreprise) in deja_vues:
                doublons.add(oid)
                continue
            offres[oid] = {
                "id": oid,
                "intitule": offre.get("title"),
                "entreprise": entreprise,
                "zone": ((j.get("workplace") or {}).get("location") or {}).get("address"),
                "teletravail": (j.get("contract") or {}).get("remote") in ("remote", "hybrid"),
                "date": (offre.get("publication") or {}).get("creation") or "",
                "url": (j.get("apply") or {}).get("url"),
            }
        time.sleep(1.2)  # sous la limite de 60 appels par minute
    print(f"La bonne alternance : {len(offres)} offres, {len(doublons)} doublons de France Travail écartés")
    return offres
