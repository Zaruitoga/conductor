"""
model/signals/quality.py — How much the inputs can be trusted, as a signal.

Running both attitude sources buys a free cross-check.  GAME_RV and RV describe
the same rigid body; they differ only by a fixed yaw offset, since one references
gravity alone and the other adds a magnetic north.  That offset should therefore
be *constant* — drifting by a degree or so a minute as the gyro yaw wanders.

When it stops being constant, the magnetometer is being lied to: a steel truss,
a raked stand, a moving light, the wheel itself.  Measuring how fast the offset
changes is a direct measurement of magnetic disturbance, with no calibration and
no assumption about the venue.

The divergence is published as its own signal rather than hidden inside the
guard, precisely so it can be watched on the scope and the threshold set against
what a real venue actually does — which is the only way to set it honestly.
"""

import math

from model.params import PARAMS
from model.quantities import ATTITUDE_ABS, ATTITUDE_REL
from model.registry import QUALITY, signal

P_RATE_TAU = PARAMS.declare(
    "mag_rate_tau_s", default=0.2, min=0.01, max=2.0, unit="s", group="magnétique",
    tau=True,
    doc="Lissage de la divergence magnétique avant de la comparer au seuil. "
        "Trop court, le bruit de quantification du capteur déclenche seul.",
)
P_DIVERGENCE_MAX = PARAMS.declare(
    "mag_divergence_max_dps", default=10.0, min=0.5, max=180.0, unit="°/s",
    group="magnétique",
    doc="Divergence à partir de laquelle la confiance magnétique tombe à zéro. "
        "La dérive gyroscopique normale est de l'ordre de 0,05 °/s ; une "
        "perturbation métallique se compte en dizaines.",
)
P_RELEASE = PARAMS.declare(
    "mag_trust_release_s", default=3.0, min=0.1, max=60.0, unit="s",
    group="magnétique", tau=True,
    doc="Temps de retour à la confiance après une perturbation. La chute est "
        "immédiate, la remontée lente : mieux vaut geler l'azimut trop "
        "longtemps que le rouvrir sur un cap encore faux.",
)
P_TRUST_MIN = PARAMS.declare(
    "mag_trust_min", default=0.5, min=0.0, max=1.0, group="magnétique",
    doc="En dessous de cette confiance, l'azimut absolu est gelé.",
)


@signal(
    "mag_divergence_dps", kind=QUALITY, unit="°/s", range=(0, 60),
    needs=(ATTITUDE_REL, ATTITUDE_ABS), params=(P_RATE_TAU,),
    doc="Vitesse de variation de l'écart entre l'attitude relative et "
        "l'attitude absolue. Devrait rester quasi nulle : ce qui monte ici est "
        "une perturbation du champ magnétique, pas un mouvement.",
)
def mag_divergence_dps(ctx):
    rel = ctx.rotation(ATTITUDE_REL)
    abs_ = ctx.rotation(ATTITUDE_ABS)
    if rel is None or abs_ is None:
        return None

    offset = rel.inv() * abs_
    previous = ctx.state.get("offset")
    ctx.state["offset"] = offset

    if previous is None or not ctx.integrable:
        return ctx.state.get("smoothed", 0.0)

    # Angle between two successive offsets: how far the "constant" moved.
    rate = math.degrees((previous.inv() * offset).magnitude()) / ctx.dt

    smoothed = ctx.state.get("smoothed", rate)
    smoothed += ctx.alpha(ctx.param(P_RATE_TAU)) * (rate - smoothed)
    ctx.state["smoothed"] = smoothed
    return smoothed


@signal(
    "mag_trust", kind=QUALITY, unit="", range=(0, 1),
    needs=(ATTITUDE_REL, ATTITUDE_ABS), depends=("mag_divergence_dps",),
    params=(P_DIVERGENCE_MAX, P_RELEASE),
    doc="Confiance dans la référence magnétique, de 0 à 1. Chute instantanément "
        "sur une perturbation, remonte lentement. Ce qui gèle l'azimut absolu.",
)
def mag_trust(ctx):
    divergence = ctx["mag_divergence_dps"]
    if divergence is None:
        return None

    target = 1.0 - divergence / max(1e-9, ctx.param(P_DIVERGENCE_MAX))
    target = min(1.0, max(0.0, target))

    trust = ctx.state.get("trust", 1.0)
    if target < trust:
        trust = target                      # a disturbance is believed at once
    else:
        trust += ctx.alpha(ctx.param(P_RELEASE)) * (target - trust)

    ctx.state["trust"] = trust
    return trust
