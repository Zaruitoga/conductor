"""
model/signals/detectors.py — The first three event detectors.

Registered the same way as a signal: importing this module is what wires them
in, via model/signals/__init__.py (imported last, after every signal they read
already exists — see model/detectors.py on why the order matters).

Each one was already named as an obvious candidate by a signal's own doc:
`accel_shock_ms2` is "the natural input for a shock detector", `motion_burst`
marks "something happening right now", and `spin_deg` advances "a full turn
per wheel turn — enough to trigger once per revolution".
"""

from model.detectors import detector, threshold
from model.params import PARAMS
from model.quantities import ACCEL, ATTITUDE_REL, OMEGA

# ── Impact ────────────────────────────────────────────────────────────────────

P_IMPACT_ON = PARAMS.declare(
    "impact_on_ms2", default=15.0, min=1.0, max=80.0, unit="m/s²", group="impact",
    doc="Écart d'accélération à sa moyenne glissante au-delà duquel un choc "
        "est déclaré.",
)
P_IMPACT_OFF = PARAMS.declare(
    "impact_off_ms2", default=8.0, min=0.5, max=79.0, unit="m/s²", group="impact",
    doc="Écart en dessous duquel le détecteur se réarme, prêt pour le "
        "prochain choc.",
)
P_IMPACT_REFRACTORY = PARAMS.declare(
    "impact_refractory_s", default=0.15, min=0.0, max=5.0, unit="s", group="impact",
    doc="Délai minimal entre deux chocs déclarés, au-delà de la seule "
        "hystérésis — pour ignorer le rebond mécanique qui suit un choc réel.",
)


@detector(
    "impact", source="accel_shock_ms2", needs=(ACCEL,),
    params=(P_IMPACT_ON, P_IMPACT_OFF, P_IMPACT_REFRACTORY),
    doc="Choc : l'accélération s'écarte brutalement de sa moyenne glissante "
        "(accel_shock_ms2). L'entrée naturelle d'un déclencheur de scène.",
)
def impact(ctx):
    return threshold("accel_shock_ms2", P_IMPACT_ON, P_IMPACT_OFF,
                      P_IMPACT_REFRACTORY)(ctx)


# ── Burst ─────────────────────────────────────────────────────────────────────

P_BURST_ON = PARAMS.declare(
    "burst_on", default=2.0, min=1.0, max=4.0, group="burst",
    doc="Rapport enveloppe rapide / lente (motion_burst) au-delà duquel un "
        "sursaut de mouvement est déclaré.",
)
P_BURST_OFF = PARAMS.declare(
    "burst_off", default=1.3, min=1.0, max=4.0, group="burst",
    doc="Rapport en dessous duquel le détecteur se réarme.",
)
P_BURST_REFRACTORY = PARAMS.declare(
    "burst_refractory_s", default=0.3, min=0.0, max=5.0, unit="s", group="burst",
    doc="Délai minimal entre deux sursauts déclarés.",
)


@detector(
    "burst", source="motion_burst", needs=(ATTITUDE_REL, OMEGA),
    params=(P_BURST_ON, P_BURST_OFF, P_BURST_REFRACTORY),
    doc="Sursaut de mouvement : l'enveloppe rapide de l'intensité prend "
        "nettement le pas sur la lente (motion_burst). Marque un changement "
        "de geste, indépendamment de son niveau absolu.",
)
def burst(ctx):
    return threshold("motion_burst", P_BURST_ON, P_BURST_OFF,
                      P_BURST_REFRACTORY)(ctx)


# ── Revolution ────────────────────────────────────────────────────────────────
# Not a threshold: a front on the wrap of an angle rather than a crossing of a
# level, which is what proves the mechanism is general and not just a
# threshold gadget.

@detector(
    "revolution", source="spin_deg", needs=(ATTITUDE_REL,),
    doc="Un tour complet de la roue autour de son axe : franchissement de la "
        "phase de rotation propre (spin_deg), une fois par tour, dans le sens "
        "de rotation.",
)
def revolution(ctx):
    value = ctx["spin_deg"]
    if value is None:
        return None
    prev = ctx.previous("spin_deg")
    if prev is None:
        return None

    delta = value - prev
    # spin_deg wraps at 360°. At any physically plausible spin rate the
    # per-tick change is a few degrees at most (100 Hz vs. up to 900 °/s on
    # omega_norm_dps ⇒ ≤ 9°/tick), so a jump this large can only be the wrap,
    # never ordinary motion within a turn.
    if delta <= -180.0:
        return {"direction": 1}     # spin increasing through 360→0: forward
    if delta >= 180.0:
        return {"direction": -1}    # spin decreasing through 0→360: backward
    return None
