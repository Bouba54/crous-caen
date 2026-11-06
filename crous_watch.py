#!/usr/bin/env python3
"""
Surveillance des logements CROUS (trouverunlogement.lescrous.fr) - zone de Caen.

Version optimisée : connexion HTTPS réutilisée (keep-alive), réponses
compressées (gzip), cycle calé sur un rythme régulier, écriture disque
uniquement quand il y a du nouveau.

Aucune installation : uniquement la bibliothèque standard de Python 3.

Lancement :
    python3 crous_watch.py

Options (variables d'environnement, toutes facultatives) :
    NTFY_TOPIC    topic ntfy.sh -> notification sur téléphone
    SEARCH_ID     force l'ID de la phase en cours (sinon détecté automatiquement)
    INTERVALLE    secondes entre deux vérifications (défaut 90, minimum 60)
    DEBUG=1       affiche les réponses brutes (utile si le site change)
"""

import gzip
import http.client
import json
import os
import random
import time
import urllib.request
from datetime import datetime

HOTE = "trouverunlogement.lescrous.fr"
FICHIER_VUS = "logements_vus.json"

# Rectangle autour de Caen : coin haut-gauche puis coin bas-droit.
ZONE_CAEN = [
    {"lon": -0.47, "lat": 49.25},
    {"lon": -0.27, "lat": 49.13},
]

INTERVALLE = max(60, int(os.environ.get("INTERVALLE", "90")))
DEBUG = os.environ.get("DEBUG") == "1"
NTFY_TOPIC = os.environ.get("NTFY_TOPIC")

ENTETES = {
    "Host": HOTE,
    "User-Agent": "Mozilla/5.0 (veille-logement-etudiant; usage personnel)",
    "Accept": "application/json",
    "Accept-Encoding": "gzip",
    "Content-Type": "application/json",
    "Connection": "keep-alive",
}


class ErreurHTTP(Exception):
    def __init__(self, code):
        super().__init__(f"HTTP {code}")
        self.code = code


class Client:
    """Client HTTPS qui garde la même connexion ouverte entre deux requêtes
    (évite de refaire à chaque fois la poignée de main TCP + TLS, lente)."""

    def __init__(self):
        self.conn = None

    def _ouvrir(self):
        self.conn = http.client.HTTPSConnection(HOTE, timeout=20)

    def requete(self, chemin, corps=None):
        donnees = json.dumps(corps, separators=(",", ":")).encode() if corps is not None else None
        methode = "POST" if corps is not None else "GET"
        # 2 essais : la connexion gardée ouverte a pu être fermée par le serveur.
        for essai in (1, 2):
            try:
                if self.conn is None:
                    self._ouvrir()
                self.conn.request(methode, chemin, body=donnees, headers=ENTETES)
                rep = self.conn.getresponse()
                brut = rep.read()  # toujours lire en entier pour pouvoir réutiliser la connexion
                if rep.getheader("Content-Encoding") == "gzip":
                    brut = gzip.decompress(brut)
                if rep.status >= 400:
                    raise ErreurHTTP(rep.status)
                texte = brut.decode("utf-8")
                if DEBUG:
                    log(f"réponse brute ({chemin}) : {texte[:500]}")
                return json.loads(texte)
            except (http.client.HTTPException, ConnectionError, TimeoutError, OSError):
                self.fermer()
                if essai == 2:
                    raise

    def fermer(self):
        if self.conn is not None:
            try:
                self.conn.close()
            finally:
                self.conn = None


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def trouver_id_phase(client):
    """Cherche l'ID de la phase en cours dans la liste publique des 'tools'."""
    if os.environ.get("SEARCH_ID"):
        return int(os.environ["SEARCH_ID"])
    outils = client.requete("/api/fr/tools")
    if isinstance(outils, dict):
        outils = outils.get("results") or outils.get("items") or outils.get("tools") or []
    ids = [int(o["id"]) for o in outils if "id" in o]
    if not ids:
        raise RuntimeError("liste des phases vide : définis SEARCH_ID à la main")
    return max(ids)  # la phase la plus récente


def corps_recherche(id_phase):
    return {
        "idTool": id_phase,
        "need_aggregation": True,
        "page": 1,
        "pageSize": 100,
        "sector": None,
        "occupationModes": [],
        "location": ZONE_CAEN,
        "residence": None,
        "precision": 6,
        "equipment": [],
        "price": {"max": 10000000},
        "area": {"min": 0},
        "adaptedPmr": False,
        "toolMechanism": "flow",
    }


def chercher(client, id_phase):
    rep = client.requete(f"/api/fr/search/{id_phase}", corps_recherche(id_phase))
    resultats = rep.get("results", rep) if isinstance(rep, dict) else {}
    return resultats.get("items", []) if isinstance(resultats, dict) else []


def decrire(logement):
    nom = (logement.get("label") or logement.get("title") or "Logement").strip()
    residence = (logement.get("residence") or {}).get("label", "")
    modes = logement.get("occupationModes") or [{}]
    prix = ((modes[0] or {}).get("rent") or {}).get("min")
    ligne = nom + (f" - {residence}" if residence else "")
    if prix:
        ligne += f" - {prix / 100:.0f} EUR/mois"
    return ligne


def notifier(titre, message):
    log(f"*** {titre} ***\n    {message}")
    print("\a", end="")
    if NTFY_TOPIC:
        try:
            req = urllib.request.Request(
                f"https://ntfy.sh/{NTFY_TOPIC}",
                data=message.encode("utf-8"),
                headers={"Title": titre, "Priority": "high"},
            )
            urllib.request.urlopen(req, timeout=8)
        except Exception as e:
            log(f"notification ntfy impossible : {e}")


def charger_vus():
    try:
        with open(FICHIER_VUS, encoding="utf-8") as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return None  # None = premier lancement


def sauver_vus(vus):
    # Écriture atomique : on écrit dans un fichier temporaire puis on remplace,
    # pour ne jamais se retrouver avec un fichier à moitié écrit.
    tmp = FICHIER_VUS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(sorted(vus), f)
    os.replace(tmp, FICHIER_VUS)


def cycle(client, etat):
    """Un tour de vérification. etat = {'vus': set|None, 'phase': int|None}."""
    if etat["phase"] is None:
        etat["phase"] = trouver_id_phase(client)
        log(f"Phase détectée : {etat['phase']}")

    logements = chercher(client, etat["phase"])
    courants = {str(l["id"]): l for l in logements if l.get("id") is not None}

    if etat["vus"] is None:  # premier passage : mémoriser sans alerter
        etat["vus"] = set(courants)
        sauver_vus(etat["vus"])
        log(f"Premier passage : {len(courants)} logement(s) déjà en ligne, mémorisés.")
        return

    nouveaux = [courants[i] for i in courants.keys() - etat["vus"]]
    if nouveaux:
        notifier(
            f"{len(nouveaux)} nouveau(x) logement(s) CROUS Caen",
            "\n".join(decrire(l) for l in nouveaux),
        )
        etat["vus"] |= courants.keys()
        sauver_vus(etat["vus"])  # on n'écrit sur le disque que s'il y a du nouveau
    else:
        log(f"Rien de nouveau ({len(courants)} logement(s) en ligne).")


def main():
    log(f"Démarrage - vérification toutes les ~{INTERVALLE}s (Ctrl+C pour arrêter)")
    client = Client()
    etat = {"vus": charger_vus(), "phase": None}
    erreurs = 0

    while True:
        debut = time.monotonic()
        try:
            cycle(client, etat)
            erreurs = 0
        except ErreurHTTP as e:
            erreurs += 1
            log(f"Erreur {e}")
            if e.code in (400, 404):
                etat["phase"] = None  # la phase a peut-être changé : on re-détecte
        except Exception as e:
            erreurs += 1
            log(f"Erreur : {e}")

        # Rythme régulier : on retire le temps déjà passé dans la requête.
        # Après des erreurs répétées, on ralentit pour ne pas harceler le site.
        cible = INTERVALLE * (1 + min(erreurs, 5))
        attente = max(5, cible - (time.monotonic() - debut)) + random.uniform(0, 10)
        time.sleep(attente)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Arrêt demandé, à bientôt.")
    finally:
        pass