"""La zone suivie : six régions et la Loire-Atlantique."""
import re
import unicodedata

# clé -> (libellé, paramètre de recherche France Travail, départements)
ZONES = {
    "idf": ("Île-de-France", {"region": "11"}, "75 77 78 91 92 93 94 95"),
    "aura": ("Auvergne-Rhône-Alpes", {"region": "84"}, "01 03 07 15 26 38 42 43 63 69 73 74"),
    "cvl": ("Centre-Val de Loire", {"region": "24"}, "18 28 36 37 41 45"),
    "occ": ("Occitanie", {"region": "76"}, "09 11 12 30 31 32 34 46 48 65 66 81 82"),
    "la": ("Loire-Atlantique", {"departement": "44"}, "44"),
    "na": ("Nouvelle-Aquitaine", {"region": "75"}, "16 17 19 23 24 33 40 47 64 79 86 87"),
    "hdf": ("Hauts-de-France", {"region": "32"}, "02 59 60 62 80"),
}
DEPARTEMENTS = {d: cle for cle, (_, _, deps) in ZONES.items() for d in deps.split()}


def departement(lieu):
    cp = lieu.get("codePostal") or ""
    if cp[:2].isdigit() and cp != "99999":
        return "2A" if cp[:2] == "20" and cp < "20200" else ("2B" if cp[:2] == "20" else cp[:2])
    m = re.match(r"\s*(\d{2}|2A|2B)\s*-", lieu.get("libelle") or "")
    return m.group(1) if m else ""


def _simple(t):
    t = unicodedata.normalize("NFD", (t or "").lower())
    return " ".join(re.findall(r"[a-z0-9]+", "".join(c for c in t if not unicodedata.combining(c))))


def region(lieu):
    """Clé de zone d'un lieu France Travail, ou None s'il est hors zone ou imprécis."""
    dep = departement(lieu)
    if dep:
        return DEPARTEMENTS.get(dep)
    nom = _simple(lieu.get("libelle"))
    return next((cle for cle, (lib, _, _) in ZONES.items() if _simple(lib) == nom), None)


def region_adresse(adresse):
    """Clé de zone d'une adresse libre, d'après son dernier code postal."""
    codes = re.findall(r"(?<!\d)\d{5}(?!\d)", adresse or "")
    return DEPARTEMENTS.get(codes[-1][:2]) if codes else None
