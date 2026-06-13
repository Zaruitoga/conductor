"""api/models.py — Pydantic request bodies for the control API."""

from pydantic import BaseModel


class HostConfig(BaseModel):
    """SET_HOST body. ip is auto-detected when omitted."""
    ip: str | None = None


class SimpleSlotConfig(BaseModel):
    """SET_SIMPLE body. Rate is given in Hz and converted to rate_us server-side."""
    slot: int
    enabled: bool
    hz: float


class SuperSlotConfig(BaseModel):
    """SET_SUPER body."""
    slot: int
    deps: list[int]
    skip: int = 1


class SessionCreate(BaseModel):
    """Open a new working session."""
    title: str
    location: str = ""
    equipment: dict = {}
    comments: str = ""
    firmware_version: str = ""


class SessionUpdate(BaseModel):
    """Patch the active session (None = leave unchanged)."""
    title: str | None = None
    location: str | None = None
    equipment: dict | None = None
    comments: str | None = None
    firmware_version: str | None = None


class TakeStart(BaseModel):
    """Start recording a take in the active session."""
    title: str = ""
    performer: str = ""
    figures: list[str] = []
    notes: str = ""


class TakeUpdate(BaseModel):
    """Patch a take's metadata after the fact (None = leave unchanged)."""
    title: str | None = None
    performer: str | None = None
    figures: list[str] | None = None
    notes: str | None = None
    video_file: str | None = None
    video_sync_time_s: float | None = None


class PlaybackRequest(BaseModel):
    """Playback start body."""
    session: str
    take: str
    speed: float = 1.0
    loop: bool = False
