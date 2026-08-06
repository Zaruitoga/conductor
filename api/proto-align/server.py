#!/usr/bin/env python3
"""PROTOTYPE JETABLE — maquette de l'interface d'alignement (issue #9).

Trois mises en page radicalement différentes sur la même page, commutées par
`?variant=A|B|C`, servies avec les VRAIES données : les .mp4 et les raw.csv de
`sessions/`, et la détection de début de mouvement de l'issue #7 recalculée à
la volée.

    python3 api/proto-align/server.py          # → http://127.0.0.1:8077/

Rien ici n'est du code de production :
  - aucune validation de chemin (le vrai endpoint vidéo devra en avoir, cf. #11) ;
  - la détection est recopiée ici au lieu de vivre dans `storage/onset.py` (#7) ;
  - l'alignement confirmé n'est stocké qu'EN MÉMOIRE, effacé au redémarrage.
"""

import csv
import json
import math
import os
import sys
from pathlib import Path

from starlette.applications import Starlette
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

HERE = Path(__file__).resolve().parent

# `sessions/` est gitignoré, donc absent d'un worktree : on remonte jusqu'au
# checkout qui en porte un.
def _find_sessions() -> Path:
    env = os.environ.get("PROTO_SESSIONS")
    if env:
        return Path(env).resolve()
    for parent in HERE.parents:
        if (parent / "sessions").is_dir():
            return parent / "sessions"
    sys.exit("Aucun dossier sessions/ trouvé — donne PROTO_SESSIONS=<chemin>.")


SESSIONS = _find_sessions()
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm"}
MIME = {".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime",
        ".webm": "video/webm"}

# Constantes de #7.
SILENCE_RAD_S = 0.5
MIN_SILENCE_S = 2.0

# Alignements confirmés — en mémoire seulement (prototype).
ALIGNED: dict[str, dict] = {}


def take_dirs():
    for sess in sorted(p for p in SESSIONS.iterdir() if p.is_dir()):
        takes = sess / "takes"
        if not takes.is_dir():
            continue
        for take in sorted(p for p in takes.iterdir() if p.is_dir()):
            yield sess.name, take


def find_video(take_dir: Path) -> Path | None:
    for p in sorted(take_dir.iterdir()):
        if p.suffix.lower() in VIDEO_EXT:
            return p
    return None


def read_curve(csv_path: Path):
    """Norme du gyro brute, en secondes depuis le 1er échantillon du take."""
    curve, t0 = [], None
    with csv_path.open(newline="") as f:
        for row in csv.reader(f):
            if not row or row[0] == "ts_rx_us":
                continue
            ts = int(row[2])
            if t0 is None:
                t0 = ts          # 1re ligne quel que soit son type : c'est la timeline de frame.t
            if row[3] == "1" and row[4]:                     # GYRO
                x, y, z = float(row[4]), float(row[5]), float(row[6])
                curve.append([round((ts - t0) / 1e6, 4),
                              round(math.sqrt(x * x + y * y + z * z), 4)])
    return curve


# #7 retient le PREMIER échantillon qui met fin à un silence d'au moins 2 s.
# On rend TOUS ceux qui satisfont la règle : le motif se produit 2 à 4 fois par
# take (roue reposée puis reprise), et départager relève de l'œil humain, pas de
# l'algorithme. La durée du repos qui précède chacun est l'indice qui les sépare.
def detect_candidates(curve):
    out, silence_start = [], None
    for t, w in curve:
        if w < SILENCE_RAD_S:
            if silence_start is None:
                silence_start = t
        else:
            if silence_start is not None and t - silence_start >= MIN_SILENCE_S:
                out.append({"t": round(t, 4), "silence_s": round(t - silence_start, 2)})
            silence_start = None
    return out


async def takes(request):
    out = []
    for session, take_dir in take_dirs():
        meta = json.loads((take_dir / "take.json").read_text())
        video = find_video(take_dir)
        key = f"{session}/{take_dir.name}"
        out.append({
            "session": session,
            "take": take_dir.name,
            "title": meta.get("title") or take_dir.name,
            "video_file": video.name if video else None,
            "video_mb": round(video.stat().st_size / 1e6, 1) if video else None,
            "packets": meta.get("packet_count"),
            "aligned": ALIGNED.get(key),
        })
    return JSONResponse(out)


async def onset(request):
    d = SESSIONS / request.query_params["session"] / "takes" / request.query_params["take"]
    curve = read_curve(d / "raw.csv")
    broken = request.query_params.get("broken")
    return JSONResponse({
        "candidates": [] if broken == "onset" else detect_candidates(curve),
        "duration_s": curve[-1][0] if curve else 0,
        "curve": curve,
        "silence_rad_s": SILENCE_RAD_S,
        "min_silence_s": MIN_SILENCE_S,
    })


async def video(request):
    d = SESSIONS / request.query_params["session"] / "takes" / request.query_params["take"]
    if request.query_params.get("broken") == "unreadable":
        return FileResponse(d / "raw.csv", media_type="video/mp4")   # décodeur en échec
    path = find_video(d)
    if path is None:
        return JSONResponse({"error": "pas de vidéo"}, status_code=404)
    return FileResponse(path, media_type=MIME.get(path.suffix.lower(), "video/mp4"))


async def align(request):
    body = await request.json()
    ALIGNED[f"{body['session']}/{body['take']}"] = {
        "onset_imu_s": body["onset_imu_s"], "onset_video_s": body["onset_video_s"]}
    return JSONResponse({"ok": True})


app = Starlette(routes=[
    Route("/api/takes", takes),
    Route("/api/onset", onset),
    Route("/api/video", video),
    Route("/api/align", align, methods=["POST"]),
    Mount("/", StaticFiles(directory=HERE, html=True)),
])

if __name__ == "__main__":
    import uvicorn
    print(f"sessions/ → {SESSIONS}")
    print("maquette  → http://127.0.0.1:8077/?variant=A")
    uvicorn.run(app, host="127.0.0.1", port=8077, log_level="warning")
