"""
api/prototype_video_sync.py — PROTOTYPE JETABLE, ticket #8.

    ⚠️  Rien ici n'est destiné à `main`.  Ce module existe pour qu'une page
        puisse afficher la vidéo d'un take pendant qu'on le rejoue, et pour
        rien d'autre.  Il sera remplacé par les tickets d'implémentation
        (endpoint vidéo, `storage/onset.py`, champs `onset_*` du take).

Ce qu'il fournit, et pourquoi il faut bien le fournir pour que le prototype
tourne :

  * `sessions/` n'est servi par aucun mount HTTP — un navigateur ne peut pas
    atteindre un fichier vidéo aujourd'hui (constat de la carte #2).  D'où un
    `FileResponse` : Starlette 1.2.1 répond déjà aux requêtes `Range`, donc le
    seek est acquis sans une ligne (recherche #5).
  * `TakeMeta` ne porte pas encore `onset_imu_s` / `onset_video_s` (décision
    #6, pas encore implémentée), et `storage/onset.py` n'existe pas (décision
    #7).  L'ancre IMU est donc recalculée ici, en vingt lignes, à partir du CSV.
    L'ancre vidéo, elle, n'est pas une donnée calculable : le prototype la fait
    saisir à la main côté navigateur (localStorage), ce qui est une béquille
    assumée — la vraie interface d'alignement est le ticket #9.

Traversée de chemin : la carte #2 signale un trou réel sur `os.path.join`.  Ce
module ne construit **jamais** de chemin à partir de ce que le client envoie —
il scanne le disque, garde un catalogue en mémoire, et le client ne fait que
désigner une entrée de ce catalogue.  Une clé inconnue est un 404, pas un
`open()`.

Les takes se trouvent sous `config`/`SESSIONS_DIR`, chemin **relatif** au
répertoire de lancement.  Depuis un worktree git, `sessions/` n'existe pas (il
est dans `.gitignore`) : poser un lien symbolique, ou passer
`PROTO_SESSIONS_DIR=/chemin/vers/sessions`.
"""

import csv
import json
import logging
import math
import os

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from storage.session_manager import SESSIONS_DIR

log = logging.getLogger("proto.video")

router = APIRouter(prefix="/api/prototype/video-sync", tags=["prototype #8"])

# Où chercher les takes. Relatif au cwd par défaut, comme SessionManager.
SESSIONS_ROOT = os.path.abspath(os.environ.get("PROTO_SESSIONS_DIR", SESSIONS_DIR))

# Type MIME explicite : la déduction de `mimetypes` dépend de la machine, et un
# type reconnu-mais-faux fait échouer le <video> là où un type absent serait
# rattrapé par la spec HTML (recherche #5).
VIDEO_MIME = {
    ".mp4":  "video/mp4",
    ".m4v":  "video/mp4",
    ".mov":  "video/quicktime",
    ".webm": "video/webm",
}

# Règle de détection du début de mouvement (décision #7). Deux constantes, et
# c'est tout : l'ancre est le premier échantillon qui met fin à un silence d'au
# moins SILENCE_S, silence = norme du gyro brute sous GYRO_QUIET_RAD_S.
GYRO_QUIET_RAD_S = 0.5
SILENCE_S        = 2.0

GYRO_TYPE_ID = 1        # 0x01 = GYRO (transport/protocol.py) — colonnes x,y,z
_WRAP_US     = 1 << 32  # ts_esp_us est un uint32

_onset_cache: dict[tuple[str, float], dict] = {}


# ── Lecture du CSV ───────────────────────────────────────────────────────────

def _gyro_curve(csv_path: str) -> tuple[list[float], list[float], float]:
    """
    Rend (t, norme du gyro, durée du take), en secondes depuis le **premier
    échantillon du take, tous types confondus** — c'est rigoureusement la
    timeline de `frame.t` (`clock.update()` tourne sur chaque paquet, pas sur
    la seule attitude ; vérifié en #6).
    """
    ts, norms = [], []
    t0 = None
    prev_raw = None
    unwrapped = 0
    last_t = 0.0

    with open(csv_path, newline="") as fh:
        for row in csv.DictReader(fh):
            raw = int(row["ts_esp_us"])
            if prev_raw is not None and raw < prev_raw - _WRAP_US // 2:
                unwrapped += _WRAP_US
            prev_raw = raw
            abs_us = raw + unwrapped
            if t0 is None:
                t0 = abs_us
            last_t = (abs_us - t0) / 1e6

            if int(row["type_id"]) != GYRO_TYPE_ID:
                continue
            try:
                gx, gy, gz = float(row["x"]), float(row["y"]), float(row["z"])
            except ValueError:      # colonne vide : la ligne n'est pas un gyro
                continue
            ts.append(last_t)
            norms.append(math.sqrt(gx * gx + gy * gy + gz * gz))

    return ts, norms, last_t


def _first_onset(ts: list[float], norms: list[float]) -> float | None:
    """
    Premier échantillon qui met fin à un silence d'au moins SILENCE_S.

    La nuance qui fait tout (#7) : c'est la **première** immobilité qui compte,
    pas la meilleure — le motif se reproduit plusieurs fois par take.
    """
    quiet_since = ts[0] if ts else None
    for t, n in zip(ts, norms):
        if n < GYRO_QUIET_RAD_S:
            if quiet_since is None:
                quiet_since = t
            continue
        if quiet_since is not None and t - quiet_since >= SILENCE_S:
            return t
        quiet_since = None
    return None


def _onset(csv_path: str) -> dict:
    """Ancre IMU d'un take, mise en cache sur (chemin, mtime)."""
    try:
        key = (csv_path, os.path.getmtime(csv_path))
    except OSError:
        return {"imu_onset_s": None, "imu_duration_s": None}
    if key in _onset_cache:
        return _onset_cache[key]

    ts, norms, duration = _gyro_curve(csv_path)
    out = {
        "imu_onset_s":    _first_onset(ts, norms),
        "imu_duration_s": round(duration, 3),
        "gyro_samples":   len(ts),
    }
    _onset_cache[key] = out
    return out


# ── Catalogue ────────────────────────────────────────────────────────────────

def _scan() -> dict[tuple[str, str], dict]:
    """
    Scanne `sessions/*/takes/*/` et rend le catalogue des takes **filmés**.

    L'import d'une vidéo est une copie manuelle dans le dossier du take
    (acquis #2 de la carte) : on la détecte donc par l'extension, sans rien
    demander à `take.json` — dont `video_file` est vide sur les takes déjà
    tournés.
    """
    catalog: dict[tuple[str, str], dict] = {}
    if not os.path.isdir(SESSIONS_ROOT):
        return catalog

    for session in sorted(os.listdir(SESSIONS_ROOT)):
        takes_dir = os.path.join(SESSIONS_ROOT, session, "takes")
        if not os.path.isdir(takes_dir):
            continue
        for take in sorted(os.listdir(takes_dir)):
            take_dir = os.path.join(takes_dir, take)
            csv_path = os.path.join(take_dir, "raw.csv")
            if not os.path.isfile(csv_path):
                continue
            video = next(
                (f for f in sorted(os.listdir(take_dir))
                 if os.path.splitext(f)[1].lower() in VIDEO_MIME),
                None,
            )
            if video is None:
                continue

            title = ""
            try:
                with open(os.path.join(take_dir, "take.json")) as fh:
                    title = json.load(fh).get("title", "")
            except (OSError, ValueError):
                pass

            path = os.path.join(take_dir, video)
            catalog[(session, take)] = {
                "session":    session,
                "take":       take,
                "title":      title,
                "video_file": video,
                "mime":       VIDEO_MIME[os.path.splitext(video)[1].lower()],
                "size":       os.path.getsize(path),
                "_path":      path,
                "_csv":       csv_path,
            }
    return catalog


@router.get("/takes")
async def list_filmed_takes() -> dict:
    """Les takes qui ont une vidéo à côté de leur CSV, avec l'ancre IMU."""
    out = []
    for entry in _scan().values():
        item = {k: v for k, v in entry.items() if not k.startswith("_")}
        item.update(_onset(entry["_csv"]))
        item["video_url"] = f"/api/prototype/video-sync/video/{entry['session']}/{entry['take']}"
        out.append(item)
    return {"root": SESSIONS_ROOT, "takes": out}


@router.get("/video/{session}/{take}")
async def take_video(session: str, take: str) -> FileResponse:
    """
    La vidéo du take. `Range` est géré par Starlette (≥ 0.39.0), donc le seek
    du navigateur est acquis sans code.

    Aucun chemin n'est construit à partir des paramètres : ils servent de clé
    dans le catalogue scanné, et une clé absente est un 404.
    """
    entry = _scan().get((session, take))
    if entry is None:
        raise HTTPException(404, f"Aucune vidéo pour {session}/{take}")
    return FileResponse(entry["_path"], media_type=entry["mime"],
                        filename=entry["video_file"])
