"""
model/signals/geometry.py — Pure functions of the current orientation.

No memory, no tuning: these values are either right or wrong.  That is what
makes them the trustworthy floor everything else is built on — a threshold you
set against `lean_deg` means the same thing next week, in another venue, on a
replay at 4×.

They are all derived from the wheel kinematics in wheel.py, computed once per
tick.
"""

import math

from model.quantities import ATTITUDE_ABS, ATTITUDE_REL
from model.registry import GEOMETRY, signal
from model.signals.wheel import heading_of, kinematics

# Horizontal extent below which a direction has no meaning: the axle is pointing
# straight up, i.e. the wheel is lying flat on the ground.
_FLAT = 1e-6


@signal(
    "lean_deg", kind=GEOMETRY, unit="deg", range=(0, 90),
    needs=(ATTITUDE_REL,),
    doc="Inclinaison du plan de la roue par rapport à la verticale. "
        "0° = roue droite, 90° = roue à plat au sol.",
)
def lean_deg(ctx):
    k = kinematics(ctx)
    if k is None:
        return None
    # atan2 of the two components rather than acos(u_perp). acos is
    # ill-conditioned exactly where it matters most: near upright its derivative
    # diverges, so float noise in u_perp becomes ~1e-6 ° of angular jitter — on
    # the very signal an artist would want to read at a tenth of a degree.
    # atan2 is well conditioned across the whole range.
    return math.degrees(math.atan2(abs(k.u[2]), k.u_perp))


@signal(
    "height_m", kind=GEOMETRY, unit="m", range=(0, 1.2),
    needs=(ATTITUDE_REL,),
    doc="Hauteur du centre de la roue au-dessus du sol. Forme close, donc "
        "sans dérive, contrairement à la position horizontale.",
)
def height_m(ctx):
    k = kinematics(ctx)
    return None if k is None else k.height


@signal(
    "spin_deg", kind=GEOMETRY, unit="deg", range=(0, 360),
    needs=(ATTITUDE_REL,),
    doc="Phase de rotation propre de la roue autour de son axe. Avance d'un "
        "tour complet par tour de roue — de quoi déclencher une fois par tour.",
)
def spin_deg(ctx):
    k = kinematics(ctx)
    if k is None or k.degenerate:
        return None
    # From the frame convention: u = [sin φ·cos λ, cos φ·cos λ, −sin λ], so the
    # spin phase falls straight out of the first two components.
    return math.degrees(math.atan2(k.u[0], k.u[1])) % 360.0


@signal(
    "tilt_dir_deg", kind=GEOMETRY, unit="deg", range=(0, 360),
    needs=(ATTITUDE_REL,),
    doc="Direction horizontale vers laquelle la roue est inclinée — c'est "
        "aussi le cap de l'axe. Repère relatif : le zéro est arbitraire et "
        "dérive lentement. Sa dérivée est la précession.",
)
def tilt_dir_deg(ctx):
    k = kinematics(ctx)
    if k is None:
        return None
    # The axle is local z. Its horizontal projection is used rather than the
    # contact offset (which points the same way) because it stays defined for an
    # upright wheel, where the contact offset collapses to zero.
    axle = k.R @ (0.0, 0.0, 1.0)
    if math.hypot(float(axle[0]), float(axle[1])) < _FLAT:
        return None       # axle vertical: the wheel is flat, no direction
    return heading_of(float(axle[0]), float(axle[1]))


@signal(
    "contact_offset_m", kind=GEOMETRY, unit="m", range=(0, 1.1),
    needs=(ATTITUDE_REL,),
    doc="Distance horizontale entre le point de contact et la verticale du "
        "centre. 0 quand la roue est droite, croît avec l'inclinaison.",
)
def contact_offset_m(ctx):
    k = kinematics(ctx)
    if k is None or k.r_contact is None:
        return None
    return float(math.hypot(k.r_contact[0], k.r_contact[1]))


@signal(
    "azimuth_deg", kind=GEOMETRY, unit="deg", range=(0, 360),
    needs=(ATTITUDE_ABS,), after=("mag_trust",),
    params=("mag_trust_min",),
    doc="Cap de l'axe dans le repère absolu (référence magnétique) : "
        "l'orientation réelle sur le plateau, et non un écart depuis un zéro "
        "arbitraire. Gelé quand le champ magnétique n'est plus fiable.",
)
def azimuth_deg(ctx):
    trust = ctx["mag_trust"]
    held  = ctx.state.get("held")

    if trust is not None and trust < ctx.param("mag_trust_min"):
        # Near steel — a truss, a raked stand, the wheel itself — the magnetic
        # yaw can swing tens of degrees without the wheel moving. Holding the
        # last trustworthy value turns that into a frozen signal, which is
        # obvious on screen, instead of a jump, which reads as a real movement.
        return held

    R = ctx.matrix(ATTITUDE_ABS)
    if R is None:
        return held
    axle = R @ (0.0, 0.0, 1.0)
    if math.hypot(float(axle[0]), float(axle[1])) < _FLAT:
        return held

    value = heading_of(float(axle[0]), float(axle[1]))
    ctx.state["held"] = value
    return value
