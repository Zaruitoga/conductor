"""
model/types.py — What the model emits, and how it reaches the wire.

Three kinds, deliberately distinct, because they do not behave the same and must
not be transported the same way:

  Frame   one per model tick — continuous values.  Behaves like a *controller*:
          it is sampled, and a missed one is of no consequence.  May be dropped
          under load; the freshest is always the one that matters.

  Event   fired at a moment — impacts, threshold crossings, figure boundaries.
          Behaves like a *trigger*: missing one or duplicating one is a fault.
          Never travels on a lossy path (see bus.py), and carries a monotonic
          `id` so a consumer can prove it missed nothing.

  Meta    schema, parameters, source changes.  Rare, and must arrive.

The raw wire packets (gyro, game_rv, super_0, heartbeat) keep flowing alongside
these as plain dicts: they are the *wire*, not the model, and downstream clients
that want them ask for them by name.
"""

from dataclasses import dataclass, field

# Bus topics. `raw` carries wire packets straight through; the other three carry
# the dataclasses below.
RAW   = "raw"
FRAME = "frame"
EVENT = "event"
META  = "meta"

KINDS = (RAW, FRAME, EVENT, META)


@dataclass(slots=True)
class Frame:
    """
    The model's continuous output for one tick.

    `pose` is kept separate from `signals` on purpose: it is the geometric state
    every visual consumer needs, it is always present, and it is not a tunable
    quantity that could be switched off.  `signals` holds the registry's values,
    where a None means "this node is enabled but could not compute" (missing
    input or a node that raised) — a distinction a renderer can act on.
    """
    t_us:    int
    seq:     int
    pose:    dict = field(default_factory=dict)   # qw qx qy qz x y z
    quality: dict = field(default_factory=dict)   # active sources, staleness, flags
    signals: dict = field(default_factory=dict)   # {name: float | None}
    states:  dict = field(default_factory=dict)   # {machine: state name}

    def to_wire(self) -> dict:
        return {
            "type":    FRAME,
            "t":       round(self.t_us / 1e6, 6),
            "seq":     self.seq,
            "pose":    self.pose,
            "quality": self.quality,
            "signals": self.signals,
            "states":  self.states,
        }


@dataclass(slots=True)
class Event:
    """
    One discrete occurrence.

    `id` is monotonic across the process lifetime and is the whole point: a
    consumer that has seen id N knows exactly what it is missing if the next one
    it sees is N+2.  That is what makes the cursor pull
    (GET /api/model/events?since=) reliable where a broadcast cannot be.
    """
    id:      int
    t_us:    int
    name:    str
    payload: dict = field(default_factory=dict)

    def to_wire(self) -> dict:
        return {
            "type":    EVENT,
            "id":      self.id,
            "t":       round(self.t_us / 1e6, 6),
            "name":    self.name,
            "payload": self.payload,
        }


@dataclass(slots=True)
class Meta:
    """A change in how the model is configured or fed. Low rate, must arrive."""
    t_us:  int
    topic: str            # "schema" | "params" | "sources" | "reset"
    data:  dict = field(default_factory=dict)

    def to_wire(self) -> dict:
        return {
            "type":  META,
            "t":     round(self.t_us / 1e6, 6),
            "topic": self.topic,
            "data":  self.data,
        }
