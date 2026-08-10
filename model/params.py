"""
model/params.py — Tunable numbers, declared where they are used, changed live.

A threshold or a time constant is not a constant: it is the thing you spend your
time adjusting, and every restart costs you the movement you had just made.  So
parameters live here rather than in config.py, which is imported by value and
therefore frozen until the next launch.

Declaration sits next to the code that reads it:

    PARAMS.declare("impact_seuil", default=2.5, min=0.1, max=20.0, unit="g",
                   doc="Amplitude au-delà de laquelle un choc est déclaré")

That single line gives the API its schema, the panel its slider with the right
bounds, and the value its clamping.  Nothing else to touch.

Profiles
--------
Values are saved as named profiles under `params/`.  A good setting found during
a rehearsal survives, can be recalled by name, and — crucially for the
regression bench — can be named explicitly when replaying a take, so a result is
always attributable to a known set of numbers.

Determinism
-----------
`revision` increments on every change.  The model reads values at the top of a
tick, so a change lands on the next sample and never mid-computation.  A replay
run is therefore reproducible as long as the profile is unchanged, which is what
the bench asserts.
"""

import json
import logging
import os
from dataclasses import dataclass, asdict

from storage.paths import confine

log = logging.getLogger("model.params")

PARAMS_DIR      = "params"
DEFAULT_PROFILE = "default"


@dataclass(frozen=True, slots=True)
class ParamSpec:
    """Everything the UI and the API need to present one tunable number."""
    name:    str
    default: float
    min:     float
    max:     float
    unit:    str = ""
    step:    float = 0.0     # 0 ⇒ let the UI pick from the range
    group:   str = ""        # which signal or detector it belongs to
    doc:     str = ""

    def clamp(self, value: float) -> float:
        return max(self.min, min(self.max, float(value)))


class ParamStore:
    """
    The declared parameters and their current values.

    Single-threaded: declared at import time, read from the model tick, written
    from route handlers — all on the event loop.
    """

    def __init__(self, directory: str = PARAMS_DIR):
        self._dir     = directory
        self._specs:  dict[str, ParamSpec] = {}
        self._values: dict[str, float]     = {}
        self.revision: int  = 0
        self.profile:  str  = DEFAULT_PROFILE

    # ── Declaration ──────────────────────────────────────────────────────────

    def declare(self, name: str, *, default: float, min: float, max: float,
                unit: str = "", step: float = 0.0, group: str = "",
                doc: str = "") -> str:
        """
        Register one tunable number. Returns its name, so a module can write
        `THRESHOLD = PARAMS.declare(...)` and use the constant afterwards.
        """
        if name in self._specs:
            raise ValueError(f"Parameter {name!r} is declared twice")
        spec = ParamSpec(name, default, min, max, unit, step, group, doc)
        self._specs[name]  = spec
        self._values[name] = spec.clamp(default)
        return name

    # ── Reading ──────────────────────────────────────────────────────────────

    def get(self, name: str) -> float:
        try:
            return self._values[name]
        except KeyError:
            raise KeyError(
                f"Parameter {name!r} was never declared — add a PARAMS.declare "
                f"call in the module that reads it"
            ) from None

    def values(self) -> dict[str, float]:
        return dict(self._values)

    # ── Writing ──────────────────────────────────────────────────────────────

    def set(self, name: str, value: float) -> float:
        """Set one value, clamped to its declared bounds. Returns what was stored."""
        spec = self._specs.get(name)
        if spec is None:
            raise KeyError(f"Unknown parameter {name!r}")
        clamped = spec.clamp(value)
        if clamped != self._values[name]:
            self._values[name] = clamped
            self.revision += 1
        return clamped

    def update(self, values: dict) -> dict[str, float]:
        """Apply several values at once. Unknown names are reported, not ignored."""
        unknown = [k for k in values if k not in self._specs]
        if unknown:
            raise KeyError(f"Unknown parameter(s): {', '.join(sorted(unknown))}")
        return {k: self.set(k, v) for k, v in values.items()}

    def reset_to_defaults(self) -> None:
        for name, spec in self._specs.items():
            self._values[name] = spec.clamp(spec.default)
        self.revision += 1

    # ── Profiles on disk ─────────────────────────────────────────────────────

    def profile_path(self, name: str) -> str:
        """
        Absolute path of one profile file. Raises UnsafePath if `name` would
        leave `params/`.

        `os.path.join` drops every component preceding an absolute one, so this
        used to *be* `/tmp/pwned.json` for `save_profile("/tmp/pwned")` — no
        `..` needed, a leading `/` was enough, and the only thing bounding it
        was the fixed `.json` suffix nobody chose as a defence.  Confining here
        rather than in the routes closes `save_profile` and `load_profile` at
        once, including callers not written yet.

        The suffix is inside the confined segment on purpose: what must stay
        under the root is the file actually opened, not the name it came from.
        """
        return confine(self._dir, f"{name}.json")

    def list_profiles(self) -> list[str]:
        """
        Every profile on disk — deliberately *not* routed through
        `profile_path()`.

        These names come from `os.listdir`, so they are not input to be
        validated: a profile saved before the shape rule existed must keep
        listing, even though loading it now answers 422.  A listing that hid it
        would make the file unreachable *and* invisible.
        """
        try:
            return sorted(
                f[:-5] for f in os.listdir(self._dir) if f.endswith(".json")
            )
        except FileNotFoundError:
            return []

    def save_profile(self, name: str) -> str:
        os.makedirs(self._dir, exist_ok=True)
        path = self.profile_path(name)
        with open(path, "w") as f:
            json.dump(self._values, f, indent=2, sort_keys=True)
        self.profile = name
        log.info(f"Parameter profile saved → {path}")
        return path

    def load_profile(self, name: str) -> dict[str, float]:
        """
        Load a profile. Values for parameters that no longer exist are dropped,
        and parameters the file does not mention keep their declared default —
        so a profile written before a new signal existed still loads cleanly.
        """
        with open(self.profile_path(name)) as f:
            stored = json.load(f)

        self.reset_to_defaults()
        applied, stale = {}, []
        for k, v in stored.items():
            if k in self._specs:
                applied[k] = self.set(k, v)
            else:
                stale.append(k)
        if stale:
            log.info(f"Profile {name!r}: ignoring {len(stale)} obsolete key(s)")
        self.profile = name
        self.revision += 1
        log.info(f"Parameter profile loaded ← {name} ({len(applied)} value(s))")
        return applied

    # ── Observation ──────────────────────────────────────────────────────────

    def schema(self) -> list[dict]:
        return [
            {**asdict(spec), "value": self._values[name]}
            for name, spec in sorted(self._specs.items())
        ]

    def snapshot(self) -> dict:
        return {
            "revision": self.revision,
            "profile":  self.profile,
            "profiles": self.list_profiles(),
            "values":   self.values(),
        }


# The one store. Signal modules declare into it at import time.
PARAMS = ParamStore()
