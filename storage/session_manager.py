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
          pose.bin               ← pose track (storage/pose_track.py)

`raw.csv` is the recording; `pose.bin` is derived from it and can always be
deleted and recomputed, which is why a take is listed on the presence of the
CSV alone.

The `takes/` subdir is explicit so session-level assets (video files…) can
live alongside it later.  The `.active` pointer is what makes the open
session survive an orchestrator restart mid-séance.
"""

import json
import logging
import os
import re
import subprocess
import unicodedata
from datetime import datetime
from dataclasses import dataclass, field, fields, asdict

from storage.paths import UnsafePath, confine, is_video_filename

log = logging.getLogger("session_manager")

SESSIONS_DIR = "sessions"
ACTIVE_FILE  = ".active"

# Session fields editable after creation (PATCH /api/session)
SESSION_EDITABLE = ("title", "location", "equipment", "comments", "firmware_version")
# Take fields editable after the fact (PATCH .../takes/{take})
TAKE_EDITABLE = ("title", "performer", "figures", "notes",
                 "video_file", "onset_imu_s", "onset_video_s")

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

    # Video alignment: the pair of anchors locating this take's start of
    # movement in its video — both of them, never their difference (ADR 0001).
    # Seconds: take-relative for the IMU one, i.e. the timeline of `frame.t`,
    # and video-relative for the other.  Indivisible, both or neither, which is
    # why "not yet aligned" needs no field of its own.
    video_file:        str = ""
    onset_imu_s:       float | None = None
    onset_video_s:     float | None = None

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
        except (FileNotFoundError, json.JSONDecodeError, TypeError, UnsafePath):
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
        """
        Patch editable fields of any take. Raises FileNotFoundError if absent.

        `video_file` is checked here rather than only at the API boundary: it is
        the one editable field that later becomes a path, so the rule belongs
        next to the write.  An empty string stays legal — that is how the field
        is cleared.
        """
        video = fields.get("video_file")
        if video not in (None, "") and not is_video_filename(video):
            raise UnsafePath(f"Not a video filename: {video!r}")

        take_dir = self.take_path(session, take)
        meta = self.load_take(take_dir)
        for k, v in fields.items():
            if k in TAKE_EDITABLE and v is not None:
                setattr(meta, k, v)
        self._write_take_meta(take_dir, meta)
        return meta

    # ── Listing / loading ───────────────────────────────────────────────────

    def list_sessions(self) -> list[dict]:
        """Full tree, newest session first: session meta + 'takes' list."""
        try:
            entries = sorted(os.listdir(self.sessions_dir), reverse=True)
        except FileNotFoundError:
            return []

        out = []
        for name in entries:
            try:
                session_dir = self.session_path(name)
            except UnsafePath:
                log.warning(f"Session {name!r} resolves outside "
                            f"{self.sessions_dir}/ — skipped")
                continue
            if not os.path.isfile(os.path.join(session_dir, "session.json")):
                continue
            try:
                meta = self.load_session(name)
            except (json.JSONDecodeError, TypeError):
                continue
            out.append({**asdict(meta), "takes": self.list_takes(name)})
        return out

    def list_takes(self, session: str) -> list[dict]:
        """
        Take metadata of one session, in index order (raw.csv required).

        The names here come from `os.listdir` of the tree, not from a request:
        nothing is validated for *shape*, so a take named by hand keeps listing.
        The consequence is deliberate — a take renamed to something the API's own
        shape rule refuses is still shown, and answers 422 on a PATCH or a replay
        rather than vanishing.  A request is judged; a directory is not.

        Only a take that resolves outside sessions/ is dropped, which costs a
        take symlinked onto another disk its listing.  That is the one case the
        two rules genuinely disagree about, and it is settled toward playback:
        the same check refuses to replay it, so listing it would promise a replay
        that will not happen.  Silence is what this loop is prone to (it already
        swallows an unreadable take.json), hence the log line.
        """
        try:
            takes_dir = os.path.join(self.session_path(session), "takes")
        except UnsafePath:
            return []
        try:
            entries = sorted(os.listdir(takes_dir))
        except FileNotFoundError:
            return []

        out = []
        for name in entries:
            try:
                take_dir = self.take_path(session, name)
            except UnsafePath:
                log.warning(f"Take {session}/{name!r} resolves outside "
                            f"{self.sessions_dir}/ — skipped")
                continue
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
        """
        Read one take's metadata, tolerant of unknown *and* missing keys.

        A take.json is written by whichever version recorded it and read by
        whichever one is running now: a field added since is missing from it, a
        field removed since is still in it.  `TakeMeta(**raw)` raises TypeError
        on the second — and `list_takes()` swallows that, so a schema change
        would make every take recorded before it *disappear from the panel*
        rather than report anything.  Filtering on the declared fields is what
        makes retiring a field cost nothing on disk.

        `name` is filled from the directory when absent, because that is where a
        take's name actually lives; take.json only echoes it, and defaulting it
        to "" would list a nameless take instead of the one that is there.  The
        dict check keeps a corrupt take.json a TypeError, the one exception
        `list_takes()` is written to skip — on a JSON array, filtering keys
        would raise AttributeError and take the whole listing down with it.
        """
        with open(os.path.join(take_dir, "take.json")) as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            raise TypeError(f"take.json is not an object: {take_dir}")
        raw.setdefault("name", os.path.basename(os.path.normpath(take_dir)))
        known = {f.name for f in fields(TakeMeta)}
        return TakeMeta(**{k: v for k, v in raw.items() if k in known})

    # ── Paths ───────────────────────────────────────────────────────────────
    # Every path built from a name goes through storage/paths.py:confine(), which
    # is what closes all the callers at once — the ones here and the ones not
    # written yet — rather than each route validating on its own.  See that
    # module for why `normpath` would not be enough.  A refusal raises
    # UnsafePath; the routes turn it into a 400, and the listings below treat it
    # like any other unreadable entry.

    def session_path(self, session: str) -> str:
        """Absolute path of one session directory. Raises UnsafePath if the name
        would leave sessions/."""
        return confine(self.sessions_dir, session)

    def take_path(self, session: str, take: str) -> str:
        """
        Absolute path of one take directory. Raises UnsafePath likewise.

        Confined twice, once per untrusted segment: a single resolution of the
        whole chain would accept `take_path("a", "..")`, which is the session's
        own directory — inside sessions/, and not a take.
        """
        return confine(os.path.join(self.session_path(session), "takes"), take)

    def video_path(self, session: str, take: str, filename: str) -> str:
        """
        Absolute path of a video file inside one take.

        `video_file` is a free string edited through PATCH, so its shape is
        checked here as well as at the API boundary, and the result is confined
        to the take's own directory — a symlink planted inside the take is the
        case the extension whitelist alone would not catch.
        """
        if not is_video_filename(filename):
            raise UnsafePath(f"Not a video filename: {filename!r}")
        return confine(self.take_path(session, take), filename)

    def csv_path(self, take_dir: str) -> str:
        return os.path.join(take_dir, "raw.csv")

    def pose_path(self, take_dir: str) -> str:
        return os.path.join(take_dir, "pose.bin")

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
