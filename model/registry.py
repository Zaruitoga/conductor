"""
model/registry.py — Declaring a signal, once, in one place.

The requirement this serves: adding a variable should make it appear in the
panel and be routable to an output without touching anything else.  So a signal
carries its own description, and everything downstream — the schema endpoint,
the scope's picker, tomorrow's OSC route list with its normalisation bounds — is
derived from that description rather than maintained in parallel.

    @signal("lean_deg", kind=GEOMETRY, unit="deg", range=(0, 90),
            needs=(ATTITUDE_REL,),
            doc="Inclinaison du plan de la roue par rapport à la verticale")
    def lean_deg(ctx):
        return degrees(acos(clamp(ctx.u_perp, 0, 1)))

Why one synchronous graph rather than a chain of async stages
--------------------------------------------------------------
The old pipeline awaited every stage for every packet.  At 100–400 Hz with
dozens of variables that is tens of thousands of coroutine round trips per
second buying nothing, since none of this does I/O.  And "ordering" pure
functions is meaningless: what actually constrains order is *dependency*, which
the declaration already states.  So execution order is derived topologically and
the whole graph runs inline within one tick.

What remains genuinely useful — and is provided — is switching a node off, and
seeing why one is unavailable.

Failure is contained at the node
---------------------------------
A node that raises yields None for its own value, increments its own counter,
and the frame goes out regardless.  A detector's bug costs its own output and
nothing else; it can never trouble the stream, which is the whole point of a
live show.
"""

import logging
import math
from dataclasses import dataclass, field
from typing import Callable

import numpy as np
from scipy.spatial.transform import Rotation

from model.params import PARAMS
from model.quantities import slots_for

log = logging.getLogger("model.registry")

# Signal families, used for grouping in the UI and for deciding how an output
# should treat a value.
GEOMETRY = "geometry"   # pure function of the current orientation: no memory, no tuning
DYNAMIC  = "dynamic"    # needs a short memory: rates, envelopes, integrals
QUALITY  = "quality"    # how much the inputs can be trusted

# One log line per node per this many failures — a node failing on every packet
# must not flood the journal during a show.
_ERROR_LOG_EVERY = 200


@dataclass(frozen=True, slots=True)
class SignalSpec:
    """A declared signal: what it is, what it needs, how to compute it."""
    name:    str
    fn:      Callable
    kind:    str
    unit:    str
    range:   tuple | None
    needs:   tuple      # canonical quantities, required
    depends: tuple      # signals it cannot be computed without
    after:   tuple      # signals it merely wants computed first
    params:  tuple      # parameter names it reads
    doc:     str

    @property
    def upstream(self) -> tuple:
        """Every signal that must run before this one, required or not."""
        return (*self.depends, *self.after)

    def describe(self) -> dict:
        return {
            "name":    self.name,
            "kind":    self.kind,
            "unit":    self.unit,
            "range":   list(self.range) if self.range else None,
            "needs":   list(self.needs),
            "depends": list(self.depends),
            "after":   list(self.after),
            "params":  list(self.params),
            "doc":     self.doc,
        }


class Context:
    """
    What a signal function is handed for one tick.

    Geometric conveniences are computed lazily and cached: most geometry signals
    want the rotation matrix and the world-up vector in local coordinates, and
    recomputing those per signal at 100 Hz would dominate the cost of the whole
    model.
    """

    __slots__ = ("resolver", "t_us", "dt", "status", "values", "prev",
                 "scratch", "_states", "_signal", "_cache")

    def __init__(self, resolver, t_us: int, dt: float, status: str,
                 values: dict, prev: dict, states: dict):
        self.resolver = resolver
        self.t_us     = t_us
        self.dt       = dt        # seconds; 0.0 when the step is not integrable
        self.status   = status
        self.values   = values    # signals computed so far this tick
        self.prev     = prev      # last tick's values
        # Per-tick shared workspace for intermediate vector quantities that are
        # not signals in their own right — the contact vector and the centre
        # velocity are each wanted by several signals and cost real work.
        self.scratch: dict = {}
        self._states  = states
        self._signal: str = ""
        self._cache: dict = {}

    # ── Identity of the running node ─────────────────────────────────────────

    def _enter(self, name: str) -> None:
        self._signal = name

    @property
    def state(self) -> dict:
        """Private, persistent storage for the running node. Cleared on reset."""
        return self._states.setdefault(self._signal, {})

    # ── Time ─────────────────────────────────────────────────────────────────

    @property
    def t(self) -> float:
        """Seconds since the start of the run. The model's only clock."""
        return self.t_us / 1e6

    @property
    def integrable(self) -> bool:
        return self.dt > 0.0

    def alpha(self, tau_s: float) -> float:
        """
        One-pole smoothing coefficient for a time constant, given this tick's dt.

        Deliberately exponential rather than a fixed fraction: it makes a tuned
        time constant independent of the sample rate, so a value found at 25 Hz
        behaves identically at 100 Hz and during a 4× replay.  A naive
        `alpha = 0.1` would silently retune itself every time the BNO
        configuration changed.
        """
        if tau_s <= 0 or self.dt <= 0:
            return 1.0
        return 1.0 - math.exp(-self.dt / tau_s)

    # ── Parameters and other signals ─────────────────────────────────────────

    def param(self, name: str) -> float:
        return PARAMS.get(name)

    def __getitem__(self, name: str):
        """Another signal's value this tick. None if it could not be computed."""
        return self.values.get(name)

    def previous(self, name: str, default=None):
        v = self.prev.get(name)
        return default if v is None else v

    # ── Raw quantities ───────────────────────────────────────────────────────

    def quantity(self, name: str) -> tuple | None:
        return self.resolver.get(name)

    def vec(self, name: str):
        """A Vec3 quantity as a numpy array, or None."""
        v = self.resolver.get(name)
        return None if v is None else np.array(v)

    def rotation(self, quantity: str) -> Rotation | None:
        """
        A quaternion quantity as a scipy Rotation, cached for this tick.

        Canonical storage is (w, x, y, z); scipy wants (x, y, z, w).  Converting
        in one place keeps that ordering trap out of every signal.
        """
        key = ("rot", quantity)
        if key not in self._cache:
            q = self.resolver.get(quantity)
            if q is None:
                self._cache[key] = None
            else:
                w, x, y, z = q
                n = math.sqrt(w * w + x * x + y * y + z * z)
                self._cache[key] = (
                    None if n == 0 else Rotation.from_quat([x / n, y / n, z / n, w / n])
                )
        return self._cache[key]

    def matrix(self, quantity: str):
        """Rotation matrix (local → world) for a quaternion quantity, cached."""
        key = ("mat", quantity)
        if key not in self._cache:
            rot = self.rotation(quantity)
            self._cache[key] = None if rot is None else rot.as_matrix()
        return self._cache[key]

    def up_local(self, quantity: str):
        """
        World "up" expressed in the sensor frame: u = Rᵀ·[0,0,1].

        The root of all the wheel geometry.  With the frame convention the
        pipeline uses (wheel plane = local xy, axle = local z), u_perp = |u_xy|
        is the cosine of the lean, which is why nearly everything starts here.
        """
        key = ("up", quantity)
        if key not in self._cache:
            m = self.matrix(quantity)
            self._cache[key] = None if m is None else m.T @ np.array([0.0, 0.0, 1.0])
        return self._cache[key]


class Registry:
    """The declared signals, ordered, and the machinery to run them safely."""

    def __init__(self):
        self._specs: dict[str, SignalSpec] = {}
        self._order: list[SignalSpec] = []
        self._dirty = True

        self.disabled: set[str] = set()
        self.errors:   dict[str, int] = {}
        self.last_error: dict[str, str] = {}

    # ── Declaration ──────────────────────────────────────────────────────────

    def add(self, spec: SignalSpec) -> None:
        if spec.name in self._specs:
            raise ValueError(f"Signal {spec.name!r} is declared twice")
        self._specs[spec.name] = spec
        self._dirty = True

    def __contains__(self, name: str) -> bool:
        return name in self._specs

    def spec(self, name: str) -> SignalSpec | None:
        return self._specs.get(name)

    def isolated(self) -> "Registry":
        """
        The same declared signals, with their own switches and error counters.

        `disabled`, `errors` and `last_error` are the only mutable state here,
        and they are *session* state, not declarations.  A batch run — the pose
        track computation, a test — that shared them would write a file whose
        contents depend on which signals someone happened to switch off in the
        Signaux tab, and would bump the counters the panel reads.  The specs
        themselves are frozen dataclasses around pure functions, so a shallow
        copy is a real separation, not a half one.
        """
        twin = Registry()
        twin._specs = dict(self._specs)
        return twin

    @property
    def names(self) -> list[str]:
        return sorted(self._specs)

    # ── Ordering ─────────────────────────────────────────────────────────────

    def order(self) -> list[SignalSpec]:
        """
        Execution order, derived from declared dependencies.

        Recomputed only when the set of signals changes, which in practice means
        once at import time.
        """
        if not self._dirty:
            return self._order

        resolved: list[SignalSpec] = []
        state: dict[str, int] = {}     # 0 = visiting, 1 = done

        def visit(name: str, trail: tuple) -> None:
            if state.get(name) == 1:
                return
            if state.get(name) == 0:
                raise ValueError(
                    "Dependency cycle between signals: "
                    + " → ".join((*trail, name))
                )
            spec = self._specs.get(name)
            if spec is None:
                raise ValueError(
                    f"Signal {trail[-1]!r} depends on {name!r}, which is not declared"
                )
            state[name] = 0
            for dep in spec.upstream:
                visit(dep, (*trail, name))
            state[name] = 1
            resolved.append(spec)

        for name in sorted(self._specs):
            visit(name, (name,))

        self._order = resolved
        self._dirty = False
        return self._order

    # ── Availability ─────────────────────────────────────────────────────────

    def availability(self, present: frozenset[str]) -> dict[str, dict]:
        """
        Per signal: can it be computed right now, and if not, exactly why.

        `present` is the set of canonical quantities actually arriving.  The
        answer propagates along dependencies, so a signal is unavailable when
        anything it stands on is — and the reason names the *root* cause, which
        is what the panel needs to print an actionable line like
        "nécessite accel — active le slot ACCEL".
        """
        out: dict[str, dict] = {}

        for spec in self.order():         # dependencies first, so lookups below hit
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

            blocked = [d for d in spec.depends if not out.get(d, {}).get("available")]
            if blocked:
                out[spec.name] = {
                    "available": False,
                    "reason": f"dépend de {', '.join(blocked)}",
                    "missing": sorted({
                        m for d in blocked for m in out.get(d, {}).get("missing", [])
                    }),
                }
                continue

            out[spec.name] = {"available": True, "reason": "", "missing": []}

        return out

    def set_enabled(self, name: str, enabled: bool) -> None:
        if name not in self._specs:
            raise KeyError(f"Unknown signal {name!r}")
        if enabled:
            self.disabled.discard(name)
        else:
            self.disabled.add(name)

    # ── Execution ────────────────────────────────────────────────────────────

    def compute(self, ctx: Context, availability: dict[str, dict]) -> dict:
        """
        Run every available node in order, returning {name: value | None}.

        A node that raises is contained: its value is None, its counter moves,
        and the tick carries on.
        """
        values = ctx.values
        for spec in self.order():
            if not availability.get(spec.name, {}).get("available"):
                values[spec.name] = None
                continue
            ctx._enter(spec.name)
            try:
                values[spec.name] = spec.fn(ctx)
            except Exception as e:
                values[spec.name] = None
                n = self.errors.get(spec.name, 0) + 1
                self.errors[spec.name] = n
                self.last_error[spec.name] = str(e)
                if n % _ERROR_LOG_EVERY == 1:
                    log.error(f"Signal {spec.name!r} raised ({n} so far): {e}")
        return values

    def reset(self) -> None:
        """Forget failure history. Node state itself is owned by the engine."""
        self.errors.clear()
        self.last_error.clear()

    # ── Observation ──────────────────────────────────────────────────────────

    def schema(self, present: frozenset[str]) -> list[dict]:
        avail = self.availability(present)
        return [
            {**spec.describe(),
             "enabled":   spec.name not in self.disabled,
             "errors":    self.errors.get(spec.name, 0),
             "last_error": self.last_error.get(spec.name),
             **avail.get(spec.name, {})}
            for spec in sorted(self._specs.values(), key=lambda s: (s.kind, s.name))
        ]


# The one registry. Signal modules declare into it at import time.
SIGNALS = Registry()


def signal(name: str, *, kind: str = GEOMETRY, unit: str = "",
           range: tuple | None = None, needs: tuple = (), depends: tuple = (),
           after: tuple = (), params: tuple = (), doc: str = ""):
    """
    Declare a signal. The decorated function is `fn(ctx) -> float | None`.

    `needs`   canonical quantities without which the signal cannot exist.
    `depends` signals it cannot be computed without — unavailability propagates.
    `after`   signals it merely wants computed first, and can do without.  The
              distinction matters: the azimuth wants the magnetic-trust guard
              ahead of it, but is perfectly computable when no guard is possible
              (nothing to cross-check against), and declaring that as a hard
              dependency would switch off a working signal for no reason.

    Returning None means "enabled, but nothing meaningful to say right now"
    (undefined heading at zero speed, for instance) — distinct from unavailable,
    which is about configuration, and from an error, which is a fault.
    """
    def decorate(fn: Callable) -> Callable:
        SIGNALS.add(SignalSpec(
            name=name, fn=fn, kind=kind, unit=unit, range=range,
            needs=tuple(needs), depends=tuple(depends), after=tuple(after),
            params=tuple(params), doc=doc,
        ))
        return fn
    return decorate
