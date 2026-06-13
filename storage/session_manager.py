"""
storage/session_manager.py — Session / Take database on disk.

A *session* is a working session (location, equipment, versions, comments)
that contains several *takes*; a take is one CSV recording with its own
metadata (title, performer, figures, notes, IMU report config).

Directory structure:

  sessions/
    .active                      ← name of the currently open session
                                   (plain text; removed on close)
    2026-06-13_14-30_trianon/    ← <date>_<time>_<slug(title)>
      session.json               ← SessionMeta
      takes/
        001_premier-essai/       ← <NNN>_<slug(take title)>
          raw.csv
          take.json              ← TakeMeta

The `takes/` subdir is explicit so session-level assets (video files…) can
live alongside it later.  The `.active` pointer is what makes the open
session survive an orchestrator restart mid-séance.
"""

import json
import os
import re
import subprocess
import unicodedata
from datetime import datetime
from dataclasses import dataclass, field, asdict

SESSIONS_DIR = "sessions"
ACTIVE_FILE  = ".active"

# Session fields editable after creation (PATCH /api/session)
SESSION_EDITABLE = ("title", "location", "equipment", "comments", "firmware_version")
# Take fields editable after the fact (PATCH .../takes/{take})
TAKE_EDITABLE = ("title", "performer", "figures", "notes",
                 "video_file", "video_sync_time_s")

_program_version_cache: str | None = None


def program_version() -> str:
    """Identify the running orchestrator from git (cached), or 'unknown'."""
    global _program_version_cache
    if _program_version_cache is None:
        try:
            _program_version_cache = subprocess.run(
                ["git", "describe", "--always", "--dirty"],
                capture_output=True, text=True, timeout=5,
                cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            ).stdout.strip() or "unknown"
        except Exception:
            _program_version_cache = "unknown"
    return _program_version_cache


def _slug(text: str) -> str:
    """ASCII lowercase slug: 'Premier essai !' → 'premier-essai'."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text


@dataclass
class SessionMeta:
    """Metadata for a working session, serialised to session.json."""
    name:             str             # directory name
    title:            str = ""
    started_at:       str = ""        # ISO 8601, auto
    ended_at:         str = ""        # stamped on close
    location:         str = ""
    equipment:        dict = field(default_factory=dict)  # imu, camera, focale, roue…
    comments:         str = ""
    firmware_version: str = ""        # manual until the ESP ACK exposes it
    program_version:  str = ""        # auto: git describe
    extra:            dict = field(default_factory=dict)


@dataclass
class TakeMeta:
    """Metadata for a single take (one CSV recording), serialised to take.json."""
    name:              str            # directory name "001_slug"
    index:             int = 0
    title:             str = ""
    performer:         str = ""
    figures:           list = field(default_factory=list)
    notes:             str = ""
    started_at:        str = ""
    ended_at:          str = ""
    first_ts_rx_us:    int = 0
    last_ts_rx_us:     int = 0
    packet_count:      int = 0
    imu_config:        dict | None = None  # configurator.state at take start

    # Video synchronisation
    video_file:        str = ""
    sync_marker_ts_us: int = 0        # ts_rx_us at the clap moment
    video_sync_time_s: float = 0.0    # position in the video at that moment

    extra:             dict = field(default_factory=dict)


class SessionManager:
    """Creates, opens, and lists sessions and their takes on disk."""

    def __init__(self, sessions_dir: str = SESSIONS_DIR):
        self.sessions_dir = sessions_dir
        os.makedirs(sessions_dir, exist_ok=True)

        # Cache of active_tree(), rebuilt after any mutation. The WS push reads
        # the active session at ~4 Hz — without this every tick would re-read
        # session.json + every take.json from disk.
        self._active_cache: dict | None = None
        self._cache_valid = False

    # ── Session lifecycle ───────────────────────────────────────────────────

    def create_session(
        self,
        title:            str,
        location:         str = "",
        equipment:        dict | None = None,
        comments:         str = "",
        firmware_version: str = "",
    ) -> SessionMeta:
        """Create a session directory, write its meta, and mark it active."""
        ts   = datetime.now().strftime("%Y-%m-%d_%H-%M")
        slug = _slug(title)
        name = f"{ts}_{slug}" if slug else ts
        session_dir = self.session_path(name)
        os.makedirs(os.path.join(session_dir, "takes"), exist_ok=True)

        meta = SessionMeta(
            name=name,
            title=title,
            started_at=datetime.now().isoformat(),
            location=location,
            equipment=equipment or {},
            comments=comments,
            firmware_version=firmware_version,
            program_version=program_version(),
        )
        self._write_session_meta(meta)
        with open(self._active_path(), "w") as f:
            f.write(name)
        return meta

    def active_session(self) -> SessionMeta | None:
        """Return the open session's meta, or None. Reads the .active pointer."""
        try:
            with open(self._active_path()) as f:
                name = f.read().strip()
            return self.load_session(name)
        except (FileNotFoundError, json.JSONDecodeError, TypeError):
            return None

    def active_tree(self) -> dict | None:
        """Active session meta + its takes as a dict, cached between mutations."""
        if not self._cache_valid:
            meta = self.active_session()
            self._active_cache = (
                None if meta is None
                else {**asdict(meta), "takes": self.list_takes(meta.name)}
            )
            self._cache_valid = True
        return self._active_cache

    def update_session(self, fields: dict) -> SessionMeta:
        """Patch editable fields of the active session. Raises if none open."""
        meta = self.active_session()
        if meta is None:
            raise RuntimeError("No active session")
        for k, v in fields.items():
            if k in SESSION_EDITABLE and v is not None:
                setattr(meta, k, v)
        self._write_session_meta(meta)
        return meta

    def close_session(self) -> SessionMeta:
        """Stamp ended_at and remove the active pointer. Raises if none open."""
        meta = self.active_session()
        if meta is None:
            raise RuntimeError("No active session")
        meta.ended_at = datetime.now().isoformat()
        self._write_session_meta(meta)
        os.remove(self._active_path())
        return meta

    # ── Takes ───────────────────────────────────────────────────────────────

    def new_take(
        self,
        title:      str = "",
        performer:  str = "",
        figures:    list | None = None,
        notes:      str = "",
        imu_config: dict | None = None,
    ) -> tuple[str, TakeMeta]:
        """
        Create the next take in the active session.
        Returns (take_dir, meta). Raises if no session is open.
        """
        session = self.active_session()
        if session is None:
            raise RuntimeError("No active session")

        index = self._next_take_index(session.name)
        title = title or f"take {index:03d}"
        slug  = _slug(title)
        name  = f"{index:03d}_{slug}" if slug else f"{index:03d}"
        take_dir = self.take_path(session.name, name)
        os.makedirs(take_dir, exist_ok=True)

        meta = TakeMeta(
            name=name,
            index=index,
            title=title,
            performer=performer,
            figures=figures or [],
            notes=notes,
            started_at=datetime.now().isoformat(),
            imu_config=imu_config,
        )
        self._write_take_meta(take_dir, meta)
        return take_dir, meta

    def close_take(self, take_dir: str, meta: TakeMeta) -> None:
        """Stamp the end time and flush take metadata to disk."""
        meta.ended_at = datetime.now().isoformat()
        self._write_take_meta(take_dir, meta)

    def update_take(self, session: str, take: str, fields: dict) -> TakeMeta:
        """Patch editable fields of any take. Raises FileNotFoundError if absent."""
        take_dir = self.take_path(session, take)
        meta = self.load_take(take_dir)
        for k, v in fields.items():
            if k in TAKE_EDITABLE and v is not None:
                setattr(meta, k, v)
        self._write_take_meta(take_dir, meta)
        return meta

    def set_sync_marker(self, take_dir: str, meta: TakeMeta, ts_rx_us: int) -> None:
        """Record the ts_rx_us of the video sync clap."""
        meta.sync_marker_ts_us = ts_rx_us
        self._write_take_meta(take_dir, meta)

    # ── Listing / loading ───────────────────────────────────────────────────

    def list_sessions(self) -> list[dict]:
        """Full tree, newest session first: session meta + 'takes' list."""
        try:
            entries = sorted(os.listdir(self.sessions_dir), reverse=True)
        except FileNotFoundError:
            return []

        out = []
        for name in entries:
            session_dir = self.session_path(name)
            if not os.path.isfile(os.path.join(session_dir, "session.json")):
                continue
            try:
                meta = self.load_session(name)
            except (json.JSONDecodeError, TypeError):
                continue
            out.append({**asdict(meta), "takes": self.list_takes(name)})
        return out

    def list_takes(self, session: str) -> list[dict]:
        """Take metadata of one session, in index order (raw.csv required)."""
        takes_dir = os.path.join(self.session_path(session), "takes")
        try:
            entries = sorted(os.listdir(takes_dir))
        except FileNotFoundError:
            return []

        out = []
        for name in entries:
            take_dir = os.path.join(takes_dir, name)
            if not os.path.isfile(self.csv_path(take_dir)):
                continue
            try:
                out.append(asdict(self.load_take(take_dir)))
            except (FileNotFoundError, json.JSONDecodeError, TypeError):
                continue
        return out

    def load_session(self, name: str) -> SessionMeta:
        with open(os.path.join(self.session_path(name), "session.json")) as f:
            return SessionMeta(**json.load(f))

    def load_take(self, take_dir: str) -> TakeMeta:
        with open(os.path.join(take_dir, "take.json")) as f:
            return TakeMeta(**json.load(f))

    # ── Paths ───────────────────────────────────────────────────────────────

    def session_path(self, session: str) -> str:
        return os.path.join(self.sessions_dir, session)

    def take_path(self, session: str, take: str) -> str:
        return os.path.join(self.sessions_dir, session, "takes", take)

    def csv_path(self, take_dir: str) -> str:
        return os.path.join(take_dir, "raw.csv")

    # ── Internals ───────────────────────────────────────────────────────────

    def _active_path(self) -> str:
        return os.path.join(self.sessions_dir, ACTIVE_FILE)

    def _next_take_index(self, session: str) -> int:
        takes_dir = os.path.join(self.session_path(session), "takes")
        try:
            entries = os.listdir(takes_dir)
        except FileNotFoundError:
            return 1
        indices = []
        for e in entries:
            m = re.match(r"^(\d+)", e)
            if m:
                indices.append(int(m.group(1)))
        return max(indices, default=0) + 1

    def _write_session_meta(self, meta: SessionMeta) -> None:
        path = os.path.join(self.session_path(meta.name), "session.json")
        with open(path, "w") as f:
            json.dump(asdict(meta), f, indent=2, ensure_ascii=False)
        self._cache_valid = False

    def _write_take_meta(self, take_dir: str, meta: TakeMeta) -> None:
        with open(os.path.join(take_dir, "take.json"), "w") as f:
            json.dump(asdict(meta), f, indent=2, ensure_ascii=False)
        self._cache_valid = False
