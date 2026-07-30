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


class ParamUpdate(BaseModel):
    """Set one or more model parameters. Values are clamped to their bounds."""
    values: dict[str, float]


class ProfileRequest(BaseModel):
    """Save or load a named parameter profile."""
    name: str


class SignalToggle(BaseModel):
    """Switch one signal on or off without restarting."""
    name: str
    enabled: bool


class OscRouteCreate(BaseModel):
    """Create one signal/event -> OSC mapping. See osc/routes.py:Route."""
    kind:     str                    # "signal" | "event"
    source:   str                    # a signal or detector name
    target:   str                    # osc/targets.py catalog key ("custom" included)
    address:  str
    args:     list[int] = []
    in_min:   float = 0.0
    in_max:   float = 1.0
    out_min:  float = 0.0
    out_max:  float = 1.0
    clamp:    bool  = True
    invert:   bool  = False
    deadband: float = 0.0
    payload_field: str | None = None
    enabled:  bool = True
    label:    str  = ""


class OscRouteUpdate(BaseModel):
    """Patch one route (None = leave unchanged)."""
    kind:     str | None = None
    source:   str | None = None
    target:   str | None = None
    address:  str | None = None
    args:     list[int] | None = None
    in_min:   float | None = None
    in_max:   float | None = None
    out_min:  float | None = None
    out_max:  float | None = None
    clamp:    bool | None = None
    invert:   bool | None = None
    deadband: float | None = None
    payload_field: str | None = None
    enabled:  bool | None = None
    label:    str | None = None


class OscSettings(BaseModel):
    """Bridge-wide settings (None = leave unchanged): the AbletonOSC target,
    the send-rate cap, and the master enable switch."""
    host:    str | None = None
    port:    int | None = None
    rate_hz: float | None = None
    enabled: bool | None = None


class OscLiveRefresh(BaseModel):
    """Re-query AbletonOSC for real names at one level of the discovery tree."""
    level:  str            # "tracks" | "devices" | "params"
    track:  int | None = None
    device: int | None = None
