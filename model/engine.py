"""
model/engine.py — The model itself: packets in, frames out.

One `Model` instance ties together the four pieces:

    clock      unwraps the ESP counter into the only timeline anything reads
    resolver   turns packets into canonical quantities, whatever the ESP config
    registry   runs the declared signals, in dependency order, each contained
    bus        publishes frames and (later) events to whoever subscribed

Nothing here knows where a packet came from.  That is the point: the live UDP
receiver, a replay paced in real time and a batch run at full tilt all call
`feed()` with the same records, so "same code live and replayed" is a structural
property rather than a discipline.

When does a tick happen
-----------------------
On the arrival of the *master* quantity — attitude, since every geometric signal
descends from it.  Other quantities are read at their latest value, and how stale
each one is at that instant is reported rather than assumed away.  Ticking on
attitude rather than on a fixed internal clock keeps latency at one packet, which
matters when the output is a stage cue.
"""

import logging

from model.bus import ModelBus
from model.clock import DISCONTINUITY, FIRST, TimeBase
from model.detectors import DETECTORS
from model.params import PARAMS
from model.quantities import (
    ATTITUDE_REL, QuantityResolver, configured_quantities,
)
from model.registry import SIGNALS, Context
from model.types import EVENT, FRAME, META, Event, Frame, Meta

# Registering the signals is a side effect of the import.
import model.signals  # noqa: F401

log = logging.getLogger("model.engine")


class Model:
    """
    Stateful interpreter of one stream. Driven from a single task.

    `reset()` must be called whenever the input stream changes — the start of a
    replay pass, the return to live — or the previous run's integrators leak
    into the new one.
    """

    def __init__(self, bus: ModelBus | None = None, max_gap_us: int = 500_000,
                 esp_state=None, registry=None, detectors=None):
        self.bus        = bus
        # Defaults to the declared registry/detectors. Overridable so a batch
        # run, or a test, can drive an isolated graph without disturbing the
        # live model.
        self.registry   = registry if registry is not None else SIGNALS
        self.detectors  = detectors if detectors is not None else DETECTORS
        self.params     = PARAMS
        self.clock      = TimeBase(max_gap_us)
        self.resolver   = QuantityResolver()
        self._max_gap_us = max_gap_us
        # Read lazily rather than stored: the ESP configuration changes through
        # commands the model knows nothing about, and a stale copy would make the
        # schema claim a sensor is configured minutes after it was switched off.
        self._esp_state = esp_state or (lambda: None)

        self._states: dict[str, dict] = {}   # per-signal/detector persistent storage
        self._prev:   dict = {}              # last tick's values
        self._availability: dict = {}
        self._detector_availability: dict = {}
        self._availability_key: tuple | None = None

        self._last_tick_us: int | None = None
        self._broken = False                 # a discontinuity since the last tick

        self.seq   = 0
        self.ticks = 0
        # Event.id is monotonic across the *process* lifetime (model/types.py),
        # unlike seq — it must NOT reset with the rest of a run, or two replay
        # passes could reuse the same ids and a consumer tracking gaps would
        # misread a real loss as none.
        self._event_id = 0

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def reset(self) -> None:
        """
        Return to a clean state. Every integrator and envelope starts over.

        `_event_id` is deliberately untouched — see its declaration above.
        """
        self.clock.reset()
        self.resolver.reset()
        self.registry.reset()
        self.detectors.reset()
        self._states.clear()
        self._prev.clear()
        self._availability = {}
        self._detector_availability = {}
        self._availability_key = None
        self._last_tick_us = None
        self._broken = False
        self.seq = 0
        self.ticks = 0
        if self.bus is not None:
            self.bus.publish(META, Meta(0, "reset", {}))
        log.info("Model reset")

    # ── Substitution: warming a twin, then taking over ───────────────────────

    def twin(self) -> "Model":
        """
        A private instance wired like this one, and deliberately bus-less.

        This is what a seek warms up (storage/seek.py): the model is re-fed a
        few seconds of take at full tilt, which makes the detectors fire, and
        those events must not reach the bus — a jump would otherwise send a
        handful of impacts into Live (ADR 0004).  No bus rather than a filter
        someone could forget to apply.

        The registries are *shared*, unlike the pose track's isolated ones
        (storage/pose_track.py).  The two want opposite things: a track must not
        depend on which signals happen to be switched off in the Signaux tab,
        while a twin is about to *become* the live model and must therefore
        honour exactly the same switches and report into the same counters.
        """
        return Model(
            bus        = None,
            max_gap_us = self._max_gap_us,
            esp_state  = self._esp_state,
            registry   = self.registry,
            detectors  = self.detectors,
        )

    def start_at(self, t_s: float) -> None:
        """
        Place the next sample at `t_s` on the timeline rather than at zero.

        During a replay the model's timeline *is* the take's — the pass opens
        with a `reset()`, so t = 0 is the take's first sample.  A warm-up that
        began mid-take and anchored at zero would have the replay resume
        reporting a `frame.t` of a few seconds while the cursor sat at thirty,
        and every consumer reading that timeline would be off by the jump.
        """
        self.clock.reset(int(t_s * 1e6))

    def continue_from(self, previous: "Model") -> None:
        """
        Take over the numbering of the model being replaced.

        `_event_id` is monotonic across the *process* lifetime (model/types.py)
        — that is what lets a consumer prove it missed nothing — and a fresh
        instance starts at zero, so a substitution would reissue ids already
        seen.  `reset()` preserves it for the same reason; here it has to be
        carried across instances by hand.

        Assignment, not a maximum: the ids the warm-up itself burned through
        never left that instance, and keeping them would open a gap a consumer
        would read as a lost event.  `seq` follows the same logic — a jump is
        the same pass continuing at another instant, not a new run.
        """
        self._event_id = previous._event_id
        self.seq       = previous.seq

    @property
    def last_tick_s(self) -> float | None:
        """
        Where this model has got to on its timeline, or None before it ticks.

        Not the same question as "what instant was asked for": a warm-up stops
        on the last row *before* the one the replay resumes on, and the seed has
        to be planted where the model actually stands — one tick of a rolling
        wheel is nine centimetres at 2 m/s.
        """
        return None if self._last_tick_us is None else self._last_tick_us / 1e6

    def node_state(self, name: str) -> dict:
        """
        One node's persistent storage — the very dict `ctx.state` hands it.

        Public because a warm-up has to plant the position integrator where the
        pose track says the wheel was: position is the one thing exponential
        forgetting never gives back (ADR 0004).  Which key holds what stays the
        node's business — see `seed_position` in model/signals/dynamics.py.
        """
        return self._states.setdefault(name, {})

    # ── The one entry point ──────────────────────────────────────────────────

    def feed(self, packet: dict) -> Frame | None:
        """
        Take one packet. Returns a Frame when it produced a tick, else None.

        Publishing on the bus happens here so every driver gets it for free;
        a driver that wants the frame without publishing (the batch bench) passes
        no bus at construction.
        """
        ts = packet.get("ts_esp_us")
        if ts is None:
            return None

        tick = self.clock.update(ts)
        if tick.status == DISCONTINUITY:
            # Remember it across packet types: a reboot seen on the gyro stream
            # invalidates the next attitude tick just as much.
            self._broken = True

        updated = self.resolver.ingest(packet, tick.t_us)
        master  = self.resolver.master()
        if master is None or master not in updated:
            return None                       # not a tick — nothing new to model

        return self._tick(tick.t_us, tick.status)

    # ── One tick ─────────────────────────────────────────────────────────────

    def _tick(self, t_us: int, clock_status: str) -> Frame:
        dt, status = self._elapsed(t_us, clock_status)

        self._refresh_availability(t_us)

        ctx = Context(self.resolver, t_us, dt, status,
                      values={}, prev=self._prev, states=self._states)
        values = self.registry.compute(ctx, self._availability)
        # Detectors read this tick's just-computed signals (ctx.values) and the
        # previous tick's (ctx.prev — still the old dict; reassigned below).
        # Run before that reassignment so ctx.previous() means what it says.
        fired = self.detectors.run(ctx, self._detector_availability)

        self.seq   += 1
        self.ticks += 1

        frame = Frame(
            t_us    = t_us,
            seq     = self.seq,
            pose    = self._pose(values),
            quality = self._quality(dt, status, t_us),
            # Only signals that *can* be computed under the current ESP config.
            # An unavailable one would otherwise be a permanent null in every
            # frame; the schema endpoint is where its absence is explained.
            signals = {name: values.get(name)
                       for name, a in self._availability.items() if a["available"]},
            states  = {},
        )
        self._prev = values

        if self.bus is not None:
            self.bus.publish(FRAME, frame)
            # Published after the frame, so a subscriber always has this tick's
            # continuous values in hand before an event that may reference them.
            for name, payload in fired:
                self._event_id += 1
                self.bus.publish(EVENT, Event(self._event_id, t_us, name, payload))
        return frame

    def _elapsed(self, t_us: int, clock_status: str) -> tuple[float, str]:
        """
        Time since the previous *tick* — not since the previous packet.

        With several streams interleaved, consecutive packets are milliseconds
        apart while consecutive ticks are one attitude period apart.  Feeding a
        signal the packet-to-packet delta would make every rate several times too
        large, so the elapsed time is measured between ticks here rather than
        taken from the clock.
        """
        last = self._last_tick_us
        self._last_tick_us = t_us

        if last is None:
            self._broken = False
            return 0.0, FIRST

        dt_us = t_us - last
        if self._broken or dt_us <= 0 or dt_us > self._max_gap_us:
            self._broken = False
            return 0.0, DISCONTINUITY

        return dt_us / 1e6, "ok"

    def _pose(self, values: dict) -> dict:
        """Orientation and position — what every visual consumer needs."""
        pose = {}
        q = self.resolver.get(ATTITUDE_REL)
        if q is not None:
            pose["qw"], pose["qx"], pose["qy"], pose["qz"] = q
        pose["x"] = values.get("pos_x")
        pose["y"] = values.get("pos_y")
        pose["z"] = values.get("pos_z")
        return pose

    def _quality(self, dt: float, status: str, t_us: int) -> dict:
        """
        Per-frame trust information, kept deliberately small.

        Only what varies tick to tick lives here; the stable part (which sensor
        supplies what) is published once on change as a `meta`, rather than
        repeated a hundred times a second.
        """
        stale = {
            q: round(age / 1000.0, 1)
            for q, age in self.resolver.staleness(t_us).items() if age > 0
        }
        return {
            "dt_ms":    round(dt * 1000.0, 3),
            "status":   status,
            # Empty when everything arrives bundled in one super slot, which is
            # the configuration to aim for.
            "stale_ms": stale,
        }

    # ── Availability ─────────────────────────────────────────────────────────

    def _refresh_availability(self, t_us: int) -> None:
        """
        Recompute which signals can run, but only when something changed.

        The answer depends on the quantities actually arriving and on which nodes
        the user switched off — both stable for minutes at a time, so this is a
        tuple comparison on the hot path and a real computation almost never.

        `t_us` matters: presence is judged on recency, so a sensor switched off
        mid-session drops out here and its signals become unavailable, instead of
        going on reporting whatever they last saw.
        """
        present = self.resolver.present(t_us)
        key = (present, frozenset(self.registry.disabled),
               frozenset(self.detectors.disabled))
        if key == self._availability_key:
            return

        self._availability_key = key
        self._availability = self.registry.availability(present)
        self._detector_availability = self.detectors.availability(present)

        sources = self.resolver.sources(t_us)
        if self.bus is not None:
            self.bus.publish(META, Meta(
                t_us, "sources",
                {
                    "sources":   sources,
                    "present":   sorted(present),
                    "available": sorted(
                        n for n, a in self._availability.items() if a["available"]
                    ),
                },
            ))
        log.info(f"Model sources: {sources}")

    # ── Observation ──────────────────────────────────────────────────────────

    def schema(self) -> dict:
        """
        Everything the panel and a future OSC bridge need to build themselves.

        `configured` and `observed` are reported separately on purpose: "the ESP
        was never told to send this" and "it was told, and it is not arriving"
        are different faults with different fixes.
        """
        now = self._last_tick_us
        present = self.resolver.present(now)
        configured = configured_quantities(self._esp_state())
        return {
            "quantities": {
                "configured": configured,
                "observed":   self.resolver.sources(now),
                # Told to send it, and it is not arriving — a different fault
                # from never having been asked, and a different fix.
                "missing":    sorted(set(configured) - set(present)),
            },
            "signals":   self.registry.schema(present),
            "detectors": self.detectors.schema(present),
            "params":    self.params.schema(),
        }

    def snapshot(self) -> dict:
        """Compact runtime state for the 4 Hz panel push."""
        return {
            "ticks":    self.ticks,
            "clock":    self.clock.stats(),
            "sources":  self.resolver.sources(self._last_tick_us),
            "params":   {"revision": self.params.revision,
                         "profile":  self.params.profile},
            "errors":   dict(self.registry.errors),
            "disabled": sorted(self.registry.disabled),
            "unavailable": sorted(
                n for n, a in self._availability.items() if not a["available"]
            ),
        }
