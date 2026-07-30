"""
osc/targets.py — The catalog of known AbletonOSC destinations.

Pure data, on purpose: a route (osc/routes.py) does not hardcode an OSC address
and argument template, it names a target from here. Adding a destination is
adding one entry, not touching any code that sends.

Every AbletonOSC address takes its index arguments *before* the value —
`/live/device/set/parameter/value` wants `[track, device, param, value]`, not
just a value — so a target is the address plus the ordered list of what an
index argument *means* (`ARG_TRACK`, `ARG_DEVICE`, ...). That list is also what
tells the panel which discovery menu to show for each slot (see
`GET /api/osc/live`, osc/live.py's discovery calls).

The addresses below match the AbletonOSC project (github.com/ideoforms/AbletonOSC)
at the time this was written. **Verify them against the AbletonOSC version
actually installed** — a mismatch costs one line here, not a redesign, which is
the point of keeping this a catalog rather than inlined strings.

`custom` is the escape hatch: a route may point anywhere, with any argument
template, including a Max for Live device or a different OSC surface entirely —
the catalog is a convenience, never a cage.
"""

from dataclasses import dataclass, field

# ── Argument placeholder kinds ────────────────────────────────────────────────
# What an index argument identifies — used by the panel to pick the right
# discovery menu (osc/live.py: track_names / device_names / parameter_names).

ARG_TRACK  = "track"
ARG_DEVICE = "device"
ARG_PARAM  = "param"
ARG_SEND   = "send"
ARG_CLIP   = "clip"
ARG_SCENE  = "scene"
ARG_FREE   = "free"     # a plain number the user types by hand — no discovery

ARG_KINDS = (ARG_TRACK, ARG_DEVICE, ARG_PARAM, ARG_SEND, ARG_CLIP, ARG_SCENE, ARG_FREE)


@dataclass(frozen=True, slots=True)
class Target:
    """One known destination: its address, its argument template, and the
    output range a mapped value should naturally land in."""
    name:       str
    address:    str | None            # None only for "custom"
    args:       tuple[str, ...]       # ARG_* placeholders, in wire order
    out:        tuple[float, float] | None   # natural output range, or None
    event_only: bool = False          # no value argument is appended (e.g. a fire)
    label:      str = ""

    def describe(self) -> dict:
        return {
            "name":       self.name,
            "address":    self.address,
            "args":       list(self.args),
            "out":        list(self.out) if self.out else None,
            "event_only": self.event_only,
            "label":      self.label,
        }


_TARGETS: dict[str, Target] = {}


def _target(name: str, address: str | None, args: tuple = (),
           out: tuple | None = None, event_only: bool = False, label: str = "") -> None:
    _TARGETS[name] = Target(name, address, args, out, event_only, label)


_target(
    "track_volume", "/live/track/set/volume", args=(ARG_TRACK,),
    out=(0.0, 1.0), label="Volume de piste",
)
_target(
    "track_panning", "/live/track/set/panning", args=(ARG_TRACK,),
    out=(-1.0, 1.0), label="Panoramique de piste",
)
_target(
    "track_send", "/live/track/set/send", args=(ARG_TRACK, ARG_SEND),
    out=(0.0, 1.0), label="Départ auxiliaire de piste",
)
_target(
    "device_parameter", "/live/device/set/parameter/value",
    args=(ARG_TRACK, ARG_DEVICE, ARG_PARAM), out=None,   # the parameter's own range
    label="Paramètre de device",
)
_target(
    "clip_fire", "/live/clip_slot/fire", args=(ARG_TRACK, ARG_CLIP),
    out=None, event_only=True, label="Déclencher un clip",
)
_target(
    "clip_stop", "/live/clip_slot/stop", args=(ARG_TRACK, ARG_CLIP),
    out=None, event_only=True, label="Arrêter un clip",
)
_target(
    "scene_fire", "/live/scene/fire", args=(ARG_SCENE,),
    out=None, event_only=True, label="Déclencher une scène",
)
_target(
    "song_tempo", "/live/song/set/tempo", args=(),
    out=(60.0, 200.0), label="Tempo du morceau",
)
_target(
    "custom", None, args=(ARG_FREE, ARG_FREE, ARG_FREE, ARG_FREE),
    out=None, label="Adresse libre",
)


def get(name: str) -> Target | None:
    return _TARGETS.get(name)


def names() -> list[str]:
    return sorted(_TARGETS)


def schema() -> list[dict]:
    """Every target, for the panel's destination picker (GET /api/osc/targets)."""
    order = [*(n for n in names() if n != "custom"), "custom"]
    return [_TARGETS[n].describe() for n in order]
