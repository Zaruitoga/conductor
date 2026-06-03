"""
storage/session_manager.py — Recording session lifecycle management.

Each session is a timestamped directory containing:
  raw.csv        — all raw UDP packets (before the pipeline)
  session.json   — metadata (config, timestamps, video sync offset…)

Directory structure:
  sessions/
    2026-05-23_14-32-10/
      raw.csv
      session.json
"""

import json
import os
from datetime import datetime
from dataclasses import dataclass, field, asdict

SESSIONS_DIR = "sessions"


@dataclass
class SessionMeta:
    """Metadata for a single recording session, serialised to session.json."""
    name:              str
    started_at:        str        # ISO 8601
    ended_at:          str = ""
    first_ts_rx_us:    int = 0
    last_ts_rx_us:     int = 0
    packet_count:      int = 0

    # Video synchronisation (filled in manually after recording)
    video_file:        str = ""
    sync_marker_ts_us: int = 0    # ts_rx_us at the clap moment
    video_sync_time_s: float = 0.0  # position in the video at that moment

    # Free-form extras for future use (e.g. R_tore, r_tore…)
    extra: dict = field(default_factory=dict)


class SessionManager:
    """Creates, closes, and lists recording sessions on disk."""

    def __init__(self, sessions_dir: str = SESSIONS_DIR):
        self.sessions_dir = sessions_dir
        os.makedirs(sessions_dir, exist_ok=True)

    def new_session(self, name: str = "") -> tuple[str, SessionMeta]:
        """
        Create a new session directory.
        Returns (session_dir, meta).
        """
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        folder_name = f"{ts}_{name}" if name else ts
        session_dir = os.path.join(self.sessions_dir, folder_name)
        os.makedirs(session_dir, exist_ok=True)

        meta = SessionMeta(
            name=folder_name,
            started_at=datetime.now().isoformat(),
        )
        self._write_meta(session_dir, meta)
        return session_dir, meta

    def close_session(self, session_dir: str, meta: SessionMeta) -> None:
        """Stamp the end time and flush metadata to disk."""
        meta.ended_at = datetime.now().isoformat()
        self._write_meta(session_dir, meta)

    def set_sync_marker(
        self,
        session_dir: str,
        meta: SessionMeta,
        ts_rx_us: int,
    ) -> None:
        """Record the ts_rx_us of the video sync clap."""
        meta.sync_marker_ts_us = ts_rx_us
        self._write_meta(session_dir, meta)

    def list_sessions(self) -> list[str]:
        """Return available session names sorted by date."""
        try:
            entries = os.listdir(self.sessions_dir)
        except FileNotFoundError:
            return []
        return sorted(
            [e for e in entries
             if os.path.isdir(os.path.join(self.sessions_dir, e))
             and os.path.exists(os.path.join(self.sessions_dir, e, "raw.csv"))]
        )

    def session_path(self, session_name: str) -> str:
        return os.path.join(self.sessions_dir, session_name)

    def csv_path(self, session_dir: str) -> str:
        return os.path.join(session_dir, "raw.csv")

    def meta_path(self, session_dir: str) -> str:
        return os.path.join(session_dir, "session.json")

    def load_meta(self, session_dir: str) -> SessionMeta:
        with open(self.meta_path(session_dir)) as f:
            d = json.load(f)
        return SessionMeta(**d)

    def _write_meta(self, session_dir: str, meta: SessionMeta) -> None:
        with open(self.meta_path(session_dir), "w") as f:
            json.dump(asdict(meta), f, indent=2)
