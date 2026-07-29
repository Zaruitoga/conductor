"""
model/signals/wheel.py — The Cyr wheel's kinematics, computed once per tick.

Frame convention, inherited from the physics and mirrored by the simulator
(simulator/motion.py): the **wheel plane is the sensor's local xy-plane** and the
**axle is local z**.  Everything below starts from

    u = Rᵀ·[0, 0, 1]        world "up", expressed in the sensor frame
    u_perp = |u_xy|         = cos(lean)

An upright wheel gives u_perp = 1.  A wheel lying flat gives u_perp = 0, and
with it the horizontal geometry becomes undefined — hence the degenerate guard.

The contact point and the centre velocity are each wanted by several signals and
cost a matrix product and a cross product, so they are computed once per tick and
shared through `ctx.scratch` rather than recomputed per signal.

Wheel dimensions are parameters rather than constants: they belong to the object
you are performing with, and swapping wheels between two takes should not mean
editing a file and restarting.
"""

import math

import numpy as np

import config
from model.params import PARAMS
from model.quantities import ATTITUDE_REL, OMEGA

# Physical dimensions of the wheel currently in use. config.py supplies the
# defaults; these are what the model actually reads.
P_WHEEL_R = PARAMS.declare(
    "wheel_R_m", default=config.R_TORE, min=0.3, max=2.5, unit="m",
    group="roue", doc="Rayon majeur de la roue (axe → centre du tube)",
)
P_WHEEL_r = PARAMS.declare(
    "wheel_r_m", default=config.r_TORE, min=0.005, max=0.2, unit="m",
    group="roue", doc="Rayon du tube de la roue",
)

# Below this, the wheel is flat enough that "which way is it leaning" and "where
# does it touch the ground" have no answer, and the formulas divide by ~zero.
_DEGENERATE = config.DEGENERATE_THRESHOLD

_UP_WORLD = np.array([0.0, 0.0, 1.0])


class Kinematics:
    """Everything geometric about this instant, derived once."""

    __slots__ = ("R", "u", "u_perp", "degenerate", "r_contact", "p_dot",
                 "wheel_R", "wheel_r")

    def __init__(self, R, u, u_perp, degenerate, r_contact, p_dot, wheel_R, wheel_r):
        self.R          = R           # rotation matrix, local → world
        self.u          = u           # world up, in local coordinates
        self.u_perp     = u_perp      # cos(lean)
        self.degenerate = degenerate
        self.r_contact  = r_contact   # world vector, centre → contact point
        self.p_dot      = p_dot       # world centre velocity, m/s (None without omega)
        self.wheel_R    = wheel_R
        self.wheel_r    = wheel_r

    @property
    def height(self) -> float:
        """Height of the centre above the ground — closed form, drift-free."""
        return self.wheel_R * self.u_perp + self.wheel_r


def kinematics(ctx) -> Kinematics | None:
    """
    Wheel geometry for this tick, computed once and cached in the tick's scratch.

    Returns None when there is no attitude to work from.
    """
    cached = ctx.scratch.get("wheel")
    if cached is not None:
        return cached

    R = ctx.matrix(ATTITUDE_REL)
    if R is None:
        return None

    u      = R.T @ _UP_WORLD
    u_perp = math.hypot(float(u[0]), float(u[1]))
    degenerate = u_perp < _DEGENERATE

    wheel_R = ctx.param(P_WHEEL_R)
    wheel_r = ctx.param(P_WHEEL_r)

    r_contact = None
    p_dot     = None

    if not degenerate:
        # World-frame vector from the torus centre to the contact point. The
        # contact sits R + r·u_perp from the axis along the leaning direction,
        # which is what makes the rolling radius depend on the lean.
        scale = (wheel_R + wheel_r * u_perp) / u_perp
        r_contact = R @ np.array([
            -scale * u[0],
            -scale * u[1],
            -wheel_r * u[2],
        ])

        omega_local = ctx.vec(OMEGA)
        if omega_local is not None:
            # No-slip constraint: the contact point is instantaneously at rest,
            # so the centre's velocity is −ω × r_contact.
            p_dot = -np.cross(R @ omega_local, r_contact)

    k = Kinematics(R, u, u_perp, degenerate, r_contact, p_dot, wheel_R, wheel_r)
    ctx.scratch["wheel"] = k
    return k


def heading_of(vx: float, vy: float) -> float:
    """Compass-style heading of a horizontal vector, in [0, 360)."""
    return math.degrees(math.atan2(vy, vx)) % 360.0


def angle_delta_deg(current: float, previous: float) -> float:
    """
    Signed shortest difference between two headings, in (−180, 180].

    Needed by every rate derived from an angle: without the wrap, a wheel
    crossing 360° would register a −360°/dt spike, which a threshold detector
    would faithfully report as a violent event.
    """
    return (current - previous + 180.0) % 360.0 - 180.0
