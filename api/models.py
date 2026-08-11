"""api/models.py — Pydantic request bodies for the control API."""

from typing import Annotated

from pydantic import BaseModel, StringConstraints, field_validator, model_validator

from storage.paths import MAX_NAME_LEN, NAME_PATTERN, VIDEO_EXTENSIONS, is_video_filename

# One directory name under sessions/, and nothing else — no separator, no `..`,
# no absolute path. Declared rather than checked in a handler, so a malformed
# name is a 422 that never reaches one, and so a route added later inherits the
# rule by writing the annotation. See storage/paths.py for the shape itself and
# for the second layer that closes the callers this one does not cover.
PathSegment = Annotated[str, StringConstraints(pattern=NAME_PATTERN,
                                               max_length=MAX_NAME_LEN)]


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
    onset_imu_s: float | None = None
    onset_video_s: float | None = None

    @model_validator(mode="after")
    def _alignment_is_indivisible(self) -> "TakeUpdate":
        """
        The two anchors are posted together or not at all.

        Half an alignment locates nothing: the offset it exists to give is the
        distance between the two, and one of them alone is just a number.
        Refusing it here is what keeps "not yet aligned" a state with no field
        of its own — either both anchors are on the take, or neither is.  A
        patch that says nothing about the alignment is untouched by the rule.
        """
        if (self.onset_imu_s is None) != (self.onset_video_s is None):
            raise ValueError(
                "onset_imu_s et onset_video_s se posent ensemble : "
                "un alignement est indivisible"
            )
        return self

    @field_validator("video_file")
    @classmethod
    def _bare_video_filename(cls, v: str | None) -> str | None:
        """
        A filename, never a path: this field is what a GET .../video would open,
        and it is the one editable string that becomes one.  "" stays legal —
        that is how the field is cleared.
        """
        if v in (None, "") or is_video_filename(v):
            return v
        raise ValueError(
            "video_file doit être un nom de fichier seul (sans / ni ..), "
            f"avec une extension vidéo : {', '.join(sorted(VIDEO_EXTENSIONS))}"
        )


class PlaybackRequest(BaseModel):
    """Playback start body."""
    session: PathSegment
    take: PathSegment
    speed: float = 1.0
    loop: bool = False


class ParamUpdate(BaseModel):
    """Set one or more model parameters. Values are clamped to their bounds."""
    values: dict[str, float]


class ProfileRequest(BaseModel):
    """
    Save or load a named profile — model parameters (`params/`) or OSC mappings
    (`mappings/`), which share this body and the same defect.

    The name is a `PathSegment` for the reason a session's is: it reaches
    `os.path.join` and therefore *becomes* the path.  Unlike a session, though,
    nothing slugifies it first — `save_profile("x")` writes `x.json` verbatim —
    so this annotation is also the only thing deciding what a profile may be
    called.  Spaces and accents are out, which is a real narrowing of a free-text
    field; the containment layer in the two `profile_path()` builders is what
    makes it defence in depth rather than the whole defence.
    """
    name: PathSegment


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
