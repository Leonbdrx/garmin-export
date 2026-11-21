"""Export quotidien Garmin -> dépôt GitHub privé (dossier donnees/).

Lancé chaque matin par .github/workflows/export-garmin.yml.
Lit la clé dans la variable d'environnement GARMIN_PERSONAL_API_KEY (secret GitHub).
Lecture seule côté Garmin : rien n'est jamais modifié sur ton compte.

Sorties dans donnees/ :
  carnet.json    synthèse par jour + séances, lue par Claude pour remplir le Carnet
  sante.csv      une ligne par jour (Excel : séparateur ;, virgule décimale)
  activites.csv  une ligne par séance
  journal.txt    une ligne par passage (séances, erreurs)
  brut/          réponses complètes de Garmin, telles quelles
  fit/           fichiers d'origine des séances (.zip contenant le .FIT)
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from garmin_personal_api import GarminPersonalApiClient, PersonalApiAuthenticator

FUSEAU = ZoneInfo("Europe/Paris")
DOSSIER = Path(__file__).resolve().parent / "donnees"
CLE_DATE = ("calendarDate", "summaryDate", "date", "calendarDay")
JOURS_MAX = 120

erreurs: list[str] = []


def erreur(message: str) -> None:
    erreurs.append(message)
    print(f"ERREUR {message}", flush=True)


def info(message: str) -> None:
    print(message, flush=True)


# ---------------------------------------------------------------- lecture JSON


def chercher(obj, cles: tuple[str, ...]):
    """Première valeur numérique trouvée pour l'une des clés, en profondeur."""
    if isinstance(obj, dict):
        for cle in cles:
            val = obj.get(cle)
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                return val
        for val in obj.values():
            trouve = chercher(val, cles)
            if trouve is not None:
                return trouve
    elif isinstance(obj, list):
        for val in obj:
            trouve = chercher(val, cles)
            if trouve is not None:
                return trouve
    return None


def date_de(obj: dict) -> str | None:
    for cle in CLE_DATE:
        val = obj.get(cle)
        if isinstance(val, str) and len(val) >= 10 and val[4] == "-" and val[7] == "-":
            return val[:10]
    return None


def par_jour(obj, acc: dict[str, list] | None = None) -> dict[str, list]:
    """Regroupe par date tous les blocs qui portent une date (calendarDate…)."""
    if acc is None:
        acc = {}
    if isinstance(obj, dict):
        jour = date_de(obj)
        if jour:
            acc.setdefault(jour, []).append(obj)
        else:
            for val in obj.values():
                par_jour(val, acc)
    elif isinstance(obj, list):
        for val in obj:
            par_jour(val, acc)
    return acc


def valeur_du_jour(blocs_par_jour: dict[str, list], jour: str, cles: tuple[str, ...], dernier=False):
    blocs = blocs_par_jour.get(jour, [])
    if dernier:
        blocs = list(reversed(blocs))
    return chercher(blocs, cles)


# ---------------------------------------------------------------- écriture


def ecrire_brut(nom: str, suffixe: str, contenu) -> None:
    chemin = DOSSIER / "brut" / nom / f"{suffixe}.json"
    chemin.parent.mkdir(parents=True, exist_ok=True)
    chemin.write_text(json.dumps(contenu, ensure_ascii=False, indent=1, sort_keys=True), "utf-8")


def avec_reprise(fonction, *args, **kwargs):
    """Garmin limite à 100 requêtes par fenêtre (HTTP 429) : on attend puis on réessaie."""
    for essai in range(6):
        try:
            return fonction(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            if getattr(exc, "status_code", None) != 429 or essai == 5:
                raise
            attente = 5
            try:
                attente = int(json.loads(exc.response_text).get("retryInSeconds", 5)) + 2
            except Exception:  # noqa: BLE001
                pass
            attente = max(attente, 5 * (essai + 1))
            info(f"  limite Garmin atteinte, pause {attente} s")
            time.sleep(attente)
    return None


def appel(nom: str, suffixe: str, fonction, *args, **kwargs):
    """Appelle Garmin ; une erreur sur une donnée n'arrête pas les autres."""
    try:
        resultat = avec_reprise(fonction, *args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - on veut tout journaliser et continuer
        erreur(f"{nom} ({suffixe}) : {type(exc).__name__} {str(exc)[:200]}")
        return None
    ecrire_brut(nom, suffixe, resultat)
    time.sleep(0.3)
    return resultat


def appel_par_tranches(nom: str, fonction, debut: date, fin: date, taille: int = 28) -> dict:
    """Garmin refuse les longues périodes (HTTP 400) : on découpe en tranches de 28 jours."""
    acc: dict[str, list] = {}
    tranche = debut
    while tranche <= fin:
        bout = min(fin, tranche + timedelta(days=taille - 1))
        par_jour(appel(nom, f"{tranche}_{bout}", fonction, tranche, bout), acc)
        tranche = bout + timedelta(days=1)
    return acc


def nombre_fr(val, decimales: int) -> str:
    if val is None:
        return ""
    if decimales == 0:
        return str(int(round(val)))
    return f"{val:.{decimales}f}".replace(".", ",")


# ---------------------------------------------------------------- séances


def categorie(type_garmin: str) -> str:
    t = type_garmin.lower()
    if any(m in t for m in ("stair", "stepper", "floor_climb")):
        return "Stepper"
    if any(m in t for m in ("climb", "boulder")):
        return "Grimpe"
    if any(m in t for m in ("run", "trail", "hik", "treadmill")):
        return "Course"
    if any(m in t for m in ("strength", "cardio", "hiit", "fitness", "training", "yoga", "pilates")):
        return "Renfo"
    return "Autre"


def type_de(act: dict) -> str:
    val = act.get("activityType")
    if isinstance(val, dict):
        return str(val.get("typeKey") or val.get("key") or "inconnu")
    return str(val or act.get("activityTypeKey") or "inconnu")


def resume_seance(act: dict) -> dict | None:
    ident = act.get("activityId") or act.get("id")
    debut = str(act.get("startTimeLocal") or act.get("startTime") or "")
    if not ident or len(debut) < 10:
        return None
    type_garmin = type_de(act)
    duree = chercher(act, ("duration", "elapsedDuration", "movingDuration"))
    distance = chercher(act, ("distance",))
    return {
        "id": int(ident),
        "date": debut[:10],
        "heure": debut[11:16],
        "nom": act.get("activityName") or "",
        "type_garmin": type_garmin,
        "categorie": categorie(type_garmin),
        "duree_min": round(duree / 60, 1) if duree else None,
        "km": round(distance / 1000, 2) if distance else None,
        "dplus_m": chercher(act, ("elevationGain", "totalAscent")),
        "fc_moy": chercher(act, ("averageHR", "averageHeartRate", "avgHr")),
        "fc_max": chercher(act, ("maxHR", "maxHeartRate")),
    }


def recuperer_seances(client, debut: date, fin: date) -> list[dict]:
    seances: list[dict] = []
    depart = 0
    while True:
        page = appel(
            "activites", f"{debut}_{fin}_p{depart}", client.search_activities,
            start=depart, limit=100, start_date=debut, end_date=fin,
        )
        if page is None:
            break
        liste = page if isinstance(page, list) else (
            page.get("activityList") or page.get("activities") or page.get("data") or []
        )
        for act in liste:
            if isinstance(act, dict):
                resume = resume_seance(act)
                if resume:
                    seances.append(resume)
        if len(liste) < 100:
            break
        depart += 100
    return seances


def telecharger_fit(client, seance: dict) -> None:
    chemin = DOSSIER / "fit" / f"{seance['date']}_{seance['id']}.zip"
    if chemin.exists():
        return
    try:
        contenu = avec_reprise(client.download_activity, seance["id"])
    except Exception as exc:  # noqa: BLE001
        erreur(f"fichier FIT {seance['id']} : {type(exc).__name__} {str(exc)[:200]}")
        return
    chemin.parent.mkdir(parents=True, exist_ok=True)
    chemin.write_bytes(contenu)
    time.sleep(0.3)


# ---------------------------------------------------------------- santé


def poids_kg(val):
    if val is None:
        return None
    return round(val / 1000, 2) if val > 500 else round(val, 2)  # Garmin donne des grammes


def recuperer_sante(client, debut: date, fin: date) -> dict[str, dict]:
    resumes = appel_par_tranches("resumes_jour", client.get_daily_summaries, debut, fin)
    vfc = appel_par_tranches("vfc", client.get_daily_hrv_summaries, debut, fin)
    poids = appel_par_tranches("poids", client.get_weight_measurements, debut, fin)
    sommeil = appel_par_tranches("sommeil", client.get_daily_sleep_summaries, debut, fin)
    stress = appel_par_tranches("stress", client.get_daily_stress, debut, fin)
    batterie = appel_par_tranches("body_battery", client.get_daily_body_battery, debut, fin)
    pas = appel_par_tranches("pas", client.get_daily_steps, debut, fin)

    jours: dict[str, dict] = {}
    jour = debut
    while jour <= fin:
        j = jour.isoformat()
        valeur_vfc = valeur_du_jour(vfc, j, ("lastNightAvg", "lastNightAverage", "hrvLastNightAvg"))
        if valeur_vfc is None and jour >= fin - timedelta(days=2):
            # Relais jour par jour sur les 3 derniers jours si le résumé est vide.
            detail = appel("vfc_jour", j, client.get_daily_hrv, j)
            valeur_vfc = chercher(detail, ("lastNightAvg", "lastNightAverage", "hrvLastNightAvg"))
        sommeil_s = valeur_du_jour(sommeil, j, ("sleepTimeSeconds", "totalSleepSeconds", "sleepTime"))
        jours[j] = {
            "vfc_nuit_ms": valeur_vfc,
            "fc_repos_bpm": valeur_du_jour(resumes, j, ("restingHeartRate", "restingHeartRateValue"))
            or valeur_du_jour(resumes, j, ("restingHr", "resting")),
            "poids_kg": poids_kg(valeur_du_jour(poids, j, ("weight", "weightInGrams"), dernier=True)),
            "sommeil_h": round(sommeil_s / 3600, 2) if sommeil_s else None,
            "stress_moyen": valeur_du_jour(
                stress, j, ("averageStressLevel", "overallStressLevel", "avgStressLevel", "value")
            ),
            "body_battery_max": valeur_du_jour(
                batterie, j, ("bodyBatteryHighestValue", "highestValue", "highest", "max")
            ),
            "pas": valeur_du_jour(pas, j, ("totalSteps", "steps", "stepCount")),
        }
        jour += timedelta(days=1)
    return jours


# ---------------------------------------------------------------- fusion et fichiers


def charger_carnet() -> dict:
    chemin = DOSSIER / "carnet.json"
    if chemin.exists():
        try:
            return json.loads(chemin.read_text("utf-8"))
        except json.JSONDecodeError:
            erreur("carnet.json illisible, il est recréé")
    return {"jours": {}, "seances": {}}


def fusionner(carnet: dict, jours: dict[str, dict], seances: list[dict]) -> None:
    for j, valeurs in jours.items():
        existant = carnet["jours"].setdefault(j, {})
        for cle, val in valeurs.items():
            if val is not None:
                existant[cle] = val  # une valeur absente aujourd'hui n'efface pas l'ancienne
    for s in seances:
        carnet["seances"][str(s["id"])] = s
    carnet["jours"] = dict(sorted(carnet["jours"].items()))
    carnet["seances"] = dict(
        sorted(carnet["seances"].items(), key=lambda kv: (kv[1]["date"], kv[1]["heure"]))
    )


def ecrire_csv(carnet: dict) -> None:
    with open(DOSSIER / "sante.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["date", "vfc_nuit_ms", "fc_repos_bpm", "poids_kg", "sommeil_h",
                    "stress_moyen", "body_battery_max", "pas"])
        for j, v in carnet["jours"].items():
            w.writerow([j, nombre_fr(v.get("vfc_nuit_ms"), 0), nombre_fr(v.get("fc_repos_bpm"), 0),
                        nombre_fr(v.get("poids_kg"), 2), nombre_fr(v.get("sommeil_h"), 2),
                        nombre_fr(v.get("stress_moyen"), 0), nombre_fr(v.get("body_battery_max"), 0),
                        nombre_fr(v.get("pas"), 0)])
    with open(DOSSIER / "activites.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["date", "heure", "categorie", "type_garmin", "nom", "duree_min", "km",
                    "dplus_m", "fc_moy", "fc_max", "id"])
        for s in carnet["seances"].values():
            w.writerow([s["date"], s["heure"], s["categorie"], s["type_garmin"], s["nom"],
                        nombre_fr(s.get("duree_min"), 1), nombre_fr(s.get("km"), 2),
                        nombre_fr(s.get("dplus_m"), 0), nombre_fr(s.get("fc_moy"), 0),
                        nombre_fr(s.get("fc_max"), 0), s["id"]])


# ---------------------------------------------------------------- principal


def main() -> int:
    parser = argparse.ArgumentParser(description="Export Garmin vers donnees/")
    parser.add_argument("--jours", type=int, default=10, help=f"jours à récupérer (1 à {JOURS_MAX})")
    args = parser.parse_args()
    nb_jours = max(1, min(JOURS_MAX, args.jours))

    cle = os.environ.get("GARMIN_PERSONAL_API_KEY", "").strip()
    if not cle:
        print("ERREUR : la clé GARMIN_PERSONAL_API_KEY est absente (Settings › Secrets › Actions)")
        return 2

    fin = datetime.now(FUSEAU).date()
    debut = fin - timedelta(days=nb_jours - 1)
    info(f"Export Garmin du {debut} au {fin} ({nb_jours} jours)")
    DOSSIER.mkdir(exist_ok=True)

    try:
        with PersonalApiAuthenticator(bearer_token_cache_file="/tmp/garmin-jeton.json") as auth:
            jeton = auth.obtain_token(cle)
    except Exception as exc:  # noqa: BLE001
        print(f"ERREUR connexion Garmin : {type(exc).__name__} {str(exc)[:300]}")
        print("Clé expirée, supprimée ou mal copiée ? Voir Dépannage.")
        return 3

    with GarminPersonalApiClient(jeton.access_token) as client:
        jours = recuperer_sante(client, debut, fin)
        seances = recuperer_seances(client, debut, fin)
        for s in seances:
            telecharger_fit(client, s)

    carnet = charger_carnet()
    fusionner(carnet, jours, seances)
    carnet["mis_a_jour"] = datetime.now(FUSEAU).isoformat(timespec="seconds")
    carnet["derniere_fenetre"] = {"debut": debut.isoformat(), "fin": fin.isoformat()}
    (DOSSIER / "carnet.json").write_text(
        json.dumps(carnet, ensure_ascii=False, indent=1), "utf-8"
    )
    ecrire_csv(carnet)

    ligne = (f"{carnet['mis_a_jour']} | fenêtre {debut} → {fin} | {len(seances)} séances | "
             f"{len(erreurs)} erreurs")
    with open(DOSSIER / "journal.txt", "a", encoding="utf-8") as f:
        f.write(ligne + "\n")
        for e in erreurs:
            f.write(f"    ERREUR {e}\n")
    info(ligne)
    return 1 if erreurs else 0


if __name__ == "__main__":
    sys.exit(main())
