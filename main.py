"""
main.py — Cyr IMU orchestrator entry point.

Launches the FastAPI control panel (api/app.py), which on startup boots all
orchestrator subsystems via core.startup():

  [LIVE]     UDPReceiver ──────────────────────┐
                                               ▼
                                            Queue ──▶ Pipeline ──▶ WSServer
                                               ▲
  [PLAYBACK] PlaybackEngine (CSV) ─────────────┘

  EspConfigurator ──UDP cfg──▶ ESP32 (config port)

The control interface (ESP config, recording, playback) is exposed as a REST
API and a web panel served at http://<host>:<API_PORT>/.  This replaces the
old stdin keyboard interface.
"""

import logging

import uvicorn

import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)


if __name__ == "__main__":
    uvicorn.run(
        "api.app:app",
        host=config.API_HOST,
        port=config.API_PORT,
        log_level="info",
    )
