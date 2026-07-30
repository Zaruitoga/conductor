"""
model/detectors.py — Declaring an event, once, in one place.

A signal answers "what is true right now"; a detector answers "did something
just happen".  Mixing the two would pollute the frame — an event is not a
continuous value, does not belong in `frame.signals`, and must not land in the
scope's ring (model/scope.py only ever sees FRAME and the reset META, by
design: an envelope is worth graphing, a trigger is worth counting).

    @detector("impact", source="accel_shock_ms2", needs=(ACCEL,),
              params=(P_IMPACT_ON, P_IMPACT_OFF, P_IMPACT_REFRACTORY),
              doc="Choc : l'accélération s'écarte brutalement de sa moyenne…")
    def impact(ctx):
        return threshold("accel_shock_ms2", P_IMPACT_ON, P_IMPACT_OFF,
                          P_IMPACT_REFRACTORY)(ctx)

A detector function is `fn(ctx) -> dict | None`.  Returning a dict fires an
event with that dict as its payload; returning None — the overwhelmingly
common case, since an event is rare by nature — means nothing happened this
tick.

Failure and availability are contained exactly like a signal's (see
model/registry.py): a detector that raises costs only its own output, and one
whose `needs` are not currently arriving simply never fires — it does not
queue up and fire the moment the sensor comes back, because there is nothing
queued: each tick asks the function fresh.

Detectors share `ctx.state` with signals, keyed by name
------------------------------------------------------
`Context.state` is `self._states.setdefault(self._signal, {})` — a single
per-name namespace the engine already reuses for both.  A detector whose name
collided with a signal's would silently corrupt that signal's own memory (an
envelope's running average, say), so `add()` refuses a name already taken by a
declared signal.  This only catches the collision in the direction that
matters here: `model/signals/__init__.py` imports every signal module before
`detectors`, so by the time a detector is declared, every signal name that
will ever exist for this check already is one.
"""

import logging
from dataclasses import dataclass
from typing import Callable

from model.quantities import slots_for
from model.registry import SIGNALS

log = logging.getLogger("model.detectors")

# One log line per detector per this many failures — see model/registry.py's
# identical reasoning: a detector failing on every tick must not flood the
# journal during a show.
_ERROR_LOG_EVERY = 200


@dataclass(frozen=True, slots=True)
class DetectorSpec:
    """A declared detector: what it watches, what it needs, how to check."""
    name:   str
    fn:     Callable
    source: str      # the signal it reads — named so the panel can say "of what"
    needs:  tuple     # canonical quantities, required
    params: tuple
    doc:    str

    def describe(self) -> dict:
        return {
            "name":   self.name,
            "source": self.source,
            "needs":  list(self.needs),
            "params": list(self.params),
            "doc":    self.doc,
        }


class DetectorRegistry:
    """The declared detectors, and the machinery to run them safely."""

    def __init__(self):
        self._specs: dict[str, DetectorSpec] = {}
        self.disabled: set[str] = set()
        self.errors:   dict[str, int] = {}
        self.last_error: dict[str, str] = {}

    # ── Declaration ──────────────────────────────────────────────────────────

    def add(self, spec: DetectorSpec) -> None:
        if spec.name in self._specs:
            raise ValueError(f"Detector {spec.name!r} is declared twice")
        if spec.name in SIGNALS:
            raise ValueError(
                f"Detector {spec.name!r} collides with a signal of the same "
                f"name — they would share ctx.state and corrupt each other"
            )
        self._specs[spec.name] = spec

    def __contains__(self, name: str) -> bool:
        return name in self._specs

    def spec(self, name: str) -> DetectorSpec | None:
        return self._specs.get(name)

    @property
    def names(self) -> list[str]:
        return sorted(self._specs)

    def set_enabled(self, name: str, enabled: bool) -> None:
        if name not in self._specs:
            raise KeyError(f"Unknown detector {name!r}")
        if enabled:
            self.disabled.discard(name)
        else:
            self.disabled.add(name)

    # ── Availability ─────────────────────────────────────────────────────────

    def availability(self, present: frozenset[str]) -> dict[str, dict]:
        """Per detector: can it run right now, and if not, exactly why."""
        out: dict[str, dict] = {}
        for spec in self._specs.values():
            if spec.name in self.disabled:
                out[spec.name] = {"available": False, "reason": "désactivé",
                                  "missing": []}
                continue
            missing = [q for q in spec.needs if q not in present]
            if missing:
                hints = "; ".join(
                    f"{q} (active {' ou '.join(slots_for(q)) or '?'})" for q in missing
                )
                out[spec.name] = {"available": False,
                                  "reason": f"nécessite {hints}",
                                  "missing": missing}
                continue
            out[spec.name] = {"available": True, "reason": "", "missing": []}
        return out

    # ── Execution ────────────────────────────────────────────────────────────

    def run(self, ctx, availability: dict[str, dict]) -> list[tuple[str, dict]]:
        """
        Run every available detector once. Returns the ones that fired, in
        declaration-sorted order, as `(name, payload)`.

        A detector that raises is contained exactly like a signal: its counter
        moves, it produced nothing this tick, and every other detector — and the
        frame itself — carries on regardless.
        """
        fired: list[tuple[str, dict]] = []
        for name in sorted(self._specs):
            if not availability.get(name, {}).get("available"):
                continue
            spec = self._specs[name]
            ctx._enter(name)
            try:
                payload = spec.fn(ctx)
            except Exception as e:
                payload = None
                n = self.errors.get(name, 0) + 1
                self.errors[name] = n
                self.last_error[name] = str(e)
                if n % _ERROR_LOG_EVERY == 1:
                    log.error(f"Detector {name!r} raised ({n} so far): {e}")
            if payload is not None:
                fired.append((name, payload))
        return fired

    def reset(self) -> None:
        """Forget failure history. Per-detector state (armed/last_fired) is
        owned by the engine's shared `_states`, cleared alongside signals'."""
        self.errors.clear()
        self.last_error.clear()

    # ── Observation ──────────────────────────────────────────────────────────

    def schema(self, present: frozenset[str]) -> list[dict]:
        avail = self.availability(present)
        return [
            {**spec.describe(),
             "enabled":    spec.name not in self.disabled,
             "errors":     self.errors.get(spec.name, 0),
             "last_error": self.last_error.get(spec.name),
             **avail.get(spec.name, {})}
            for spec in sorted(self._specs.values(), key=lambda s: s.name)
        ]


# The one registry. Detector modules declare into it at import time.
DETECTORS = DetectorRegistry()


def detector(name: str, *, source: str = "", needs: tuple = (),
             params: tuple = (), doc: str = ""):
    """
    Declare a detector. The decorated function is `fn(ctx) -> dict | None`.

    `source` names the signal the detector reads, for the panel to explain
    what an event is "of". `needs` are the canonical quantities without which
    it cannot run — normally the same as the source signal's own `needs`,
    since a detector reading an unavailable signal only ever sees None anyway;
    declaring them here lets the detector be skipped rather than invoked for
    nothing on every tick.
    """
    def decorate(fn: Callable) -> Callable:
        DETECTORS.add(DetectorSpec(
            name=name, fn=fn, source=source, needs=tuple(needs),
            params=tuple(params), doc=doc,
        ))
        return fn
    return decorate


def threshold(source: str, on: str, off: str, refractory: str,
              payload: Callable | None = None):
    """
    Build a detector function with hysteresis and a cooldown.

    `on`/`off`/`refractory` are *parameter names* (declared via
    `PARAMS.declare`), read fresh every tick — a threshold is retunable live,
    mid-show, exactly like anything else PARAMS governs. `off` should sit below
    `on` for the detector to ever re-arm; that ordering is the caller's
    responsibility, not enforced here.

    Fires once per crossing: after firing, re-arming requires a drop to `off`
    or below. Once re-armed, `refractory` further blocks a firing that would
    land sooner than that many seconds after the last one — the value may dip
    to `off` and cross back over `on` almost immediately, and one meaningful
    trigger is rarely just one sample above the line. A value that never dips
    to `off` at all never re-arms in the first place, so `refractory` never
    even needs to compare — hysteresis alone already accounts for it.

    `payload(ctx, value)` builds the event's payload; the default is
    `{"value": value}`.
    """
    build_payload = payload or (lambda ctx, value: {"value": value})

    def fn(ctx):
        value = ctx[source]
        if value is None:
            return None

        st = ctx.state
        armed = st.setdefault("armed", True)

        if not armed:
            if value <= ctx.param(off):
                st["armed"] = True
            return None

        if value < ctx.param(on):
            return None

        last = st.get("last_fired")
        if last is not None and (ctx.t - last) < ctx.param(refractory):
            # Still cooling down. Stay armed rather than disarm-without-firing:
            # the instant the cooldown elapses, a value still above `on` fires
            # immediately instead of waiting for a fresh crossing.
            return None

        st["armed"] = False
        st["last_fired"] = ctx.t
        return build_payload(ctx, value)

    return fn
