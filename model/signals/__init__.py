"""
model/signals/ — The declared signals.

Importing this package is what registers everything: each module's `@signal`
decorators and `PARAMS.declare` calls run at import time.  Adding a new module
here is the only wiring a new family of signals needs — the schema endpoint, the
scope's picker and the parameter panel all build themselves from the registry.

  wheel.py      shared Cyr-wheel kinematics, computed once per tick
  geometry.py   pure functions of the current orientation — no memory, no tuning
  dynamics.py   rates, integrals, envelopes — short memory, tunable constants
  quality.py    how far the inputs can be trusted
  detectors.py  events built on top of the signals above — imported *last*,
                since model/detectors.py refuses a detector name that collides
                with a signal, and can only check against signals already
                declared.
"""

from model.signals import wheel      # noqa: F401  (kinematics helpers)
from model.signals import quality    # noqa: F401  (declares mag_trust first)
from model.signals import geometry   # noqa: F401
from model.signals import dynamics   # noqa: F401
from model.signals import detectors  # noqa: F401
