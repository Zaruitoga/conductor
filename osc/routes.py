"""
osc/routes.py — The signal→OSC mapping table: CRUD, and named profiles on disk.

A route says "take this source, send it to this OSC address with this argument
template, transformed this way". The whole point of this module is that the
mapping lives here — in a JSON-backed table edited through the API and the
panel — and nowhere near the code that declares a signal or a detector.

Two different kinds of validity, checked at two different times
------------------------------------------------------------------
*Structural* validity (types, sane bounds — `in_min != in_max`, `deadband >=
0`) is a caller mistake regardless of what the model is doing right now, so it
is checked here and enforced at create/update time.

Whether a route's `source` still names a signal or detector that *currently
exists* is a different, time-varying question — exactly like a signal's own
availability (model/registry.py), it is answered fresh on every read, via
`schema()`, never cached on the route. A profile written before a signal was
renamed still loads in full; its route simply reports why it cannot fire,
the same way the model schema explains an unavailable signal rather than
hiding it.
"""

import json
import logging
import os
import uuid
from dataclasses import asdict, dataclass, replace

from storage.paths import confine

log = logging.getLogger("osc.routes")

MAPPINGS_DIR    = "mappings"
DEFAULT_PROFILE = "default"

KIND_SIGNAL = "signal"
KIND_EVENT  = "event"
KINDS = (KIND_SIGNAL, KIND_EVENT)


@dataclass(slots=True, kw_only=True)
class Route:
    """One mapping. `args` are concrete index values, positional, matching the
    target's argument template (osc/targets.py) — e.g. `[2, 0, 7]` for track 2,
    device 0, parameter 7."""
    id:       str
    kind:     str                       # "signal" | "event"
    source:   str                       # a signal or detector name
    target:   str                       # osc.targets catalog key ("custom" included)
    address:  str                       # the OSC address actually sent
    args:     list
    in_min:   float = 0.0
    in_max:   float = 1.0
    out_min:  float = 0.0
    out_max:  float = 1.0
    clamp:    bool  = True
    invert:   bool  = False
    deadband: float = 0.0
    # kind="event": which payload key carries the value, if any — None means
    # the route only fires (a clip, a scene), it carries no value at all.
    payload_field: str | None = None
    enabled:  bool = True
    label:    str  = ""                 # free-text note, e.g. "volume piste 3"

    def describe(self) -> dict:
        return asdict(self)


def _validate(route: Route) -> None:
    if route.kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    if not route.source:
        raise ValueError("source is required")
    if not route.address:
        raise ValueError("address is required")
    if route.in_min == route.in_max:
        raise ValueError("in_min and in_max must differ")
    if route.out_min == route.out_max:
        raise ValueError("out_min and out_max must differ")
    if route.deadband < 0:
        raise ValueError("deadband must be >= 0")


class RouteTable:
    """The stored routes, plus profile persistence under `mappings/`."""

    def __init__(self, directory: str = MAPPINGS_DIR):
        self._dir = directory
        self._routes: dict[str, Route] = {}
        self.revision = 0
        self.profile  = DEFAULT_PROFILE

    # ── CRUD ─────────────────────────────────────────────────────────────────

    def all(self) -> list[Route]:
        return sorted(self._routes.values(), key=lambda r: r.id)

    def get(self, route_id: str) -> Route | None:
        return self._routes.get(route_id)

    def create(self, **fields) -> Route:
        route = Route(id=uuid.uuid4().hex[:8], **fields)
        _validate(route)
        self._routes[route.id] = route
        self.revision += 1
        return route

    def update(self, route_id: str, **fields) -> Route:
        current = self._routes.get(route_id)
        if current is None:
            raise KeyError(f"Unknown route {route_id!r}")
        route = replace(current, **fields)
        _validate(route)
        self._routes[route_id] = route
        self.revision += 1
        return route

    def delete(self, route_id: str) -> None:
        if route_id not in self._routes:
            raise KeyError(f"Unknown route {route_id!r}")
        del self._routes[route_id]
        self.revision += 1

    # ── Observation ──────────────────────────────────────────────────────────

    def schema(self, known_signals: frozenset = frozenset(),
              known_detectors: frozenset = frozenset()) -> list[dict]:
        """Every route, annotated with whether its source still exists."""
        out = []
        for route in self.all():
            known = known_signals if route.kind == KIND_SIGNAL else known_detectors
            valid = route.source in known
            out.append({
                **route.describe(),
                "valid":  valid,
                "reason": "" if valid else f"source introuvable : {route.source}",
            })
        return out

    # ── Profiles on disk ─────────────────────────────────────────────────────
    # Same shape as model/params.py's profile store: named JSON snapshots under
    # a directory, one active at a time.

    def profile_path(self, name: str) -> str:
        """
        Absolute path of one mapping file, confined to `mappings/` — same
        builder, same defect and same fix as `ParamStore.profile_path()`; see
        the reasoning there.
        """
        return confine(self._dir, f"{name}.json")

    def list_profiles(self) -> list[str]:
        """Every mapping on disk. Not routed through `profile_path()`: these
        names come from `os.listdir` and are not input to validate."""
        try:
            return sorted(f[:-5] for f in os.listdir(self._dir) if f.endswith(".json"))
        except FileNotFoundError:
            return []

    def save_profile(self, name: str) -> str:
        os.makedirs(self._dir, exist_ok=True)
        path = self.profile_path(name)
        with open(path, "w") as f:
            json.dump([r.describe() for r in self.all()], f, indent=2, sort_keys=True)
        self.profile = name
        log.info(f"OSC mapping saved → {path}")
        return path

    def load_profile(self, name: str) -> list[Route]:
        """
        Load a profile, tolerantly: a row whose `source` no longer names a live
        signal or detector still loads — see the module docstring, this is a
        model-state question answered fresh by `schema()`, not a load-time one.
        Only a genuinely malformed row (missing/renamed dataclass field, from a
        profile written by an older version) is skipped rather than crashing
        the whole load.
        """
        with open(self.profile_path(name)) as f:
            stored = json.load(f)

        routes: dict[str, Route] = {}
        skipped = 0
        for fields in stored:
            try:
                route = Route(**fields)
            except TypeError as e:
                skipped += 1
                log.warning(f"OSC mapping {name!r}: skipping a malformed route: {e}")
                continue
            routes[route.id] = route

        self._routes = routes
        self.profile = name
        self.revision += 1
        log.info(
            f"OSC mapping loaded ← {name} ({len(routes)} route(s)"
            f"{f', {skipped} skipped' if skipped else ''})"
        )
        return self.all()

    def snapshot(self) -> dict:
        return {
            "revision": self.revision,
            "profile":  self.profile,
            "profiles": self.list_profiles(),
            "count":    len(self._routes),
        }
