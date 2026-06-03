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


class PlaybackRequest(BaseModel):
    """Playback start body."""
    name: str
    speed: float = 1.0
