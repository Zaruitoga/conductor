"""
model/signals/dynamics.py — Values that need a short memory.

Rates, integrals and envelopes.  Unlike geometry, these have tunable time
constants — but every one of them is written in *dt-aware* form
(`ctx.alpha(tau)`), so a constant found at 25 Hz behaves identically at 100 Hz
and during a 4× replay.  A naive fixed coefficient would silently retune itself
every time the BNO configuration changed, which would make the whole idea of
tuning against recordings worthless.

Position is the one integral here, and it drifts by construction: it is the
Euler integration of the no-slip rolling constraint, with no absolute reference
to correct it.  The height does not drift (closed form, see geometry.py), and
that asymmetry is worth remembering before mapping px/py to anything that must
still be right ten minutes into a show.
"""

import math

from model.params import PARAMS
from model.quantities import ACCEL, ATTITUDE_REL, OMEGA
from model.registry import DYNAMIC, signal
from model.signals.wheel import angle_delta_deg, heading_of, kinematics

P_MOTION_TAU_FAST = PARAMS.declare(
    "motion_tau_fast_s", default=0.12, min=0.01, max=2.0, unit="s", group="énergie",
    tau=True,
    doc="Constante de temps de l'enveloppe rapide du mouvement — ce qui suit "
        "le geste.",
)
P_MOTION_TAU_SLOW = PARAMS.declare(
    "motion_tau_slow_s", default=2.5, min=0.1, max=30.0, unit="s", group="énergie",
    tau=True,
    doc="Constante de temps de l'enveloppe lente — le niveau général de la "
        "séquence, dont la rapide se détache.",
)
P_MIN_SPEED_HEADING = PARAMS.declare(
    "heading_min_speed_ms", default=0.15, min=0.0, max=3.0, unit="m/s",
    group="déplacement",
    doc="En dessous de cette vitesse, le cap de déplacement n'a plus de sens "
        "et il est gelé plutôt que de partir dans le bruit.",
)
P_RATE_TAU = PARAMS.declare(
    "rate_tau_s", default=0.05, min=0.0, max=1.0, unit="s", group="dérivées",
    tau=True,
    doc="Lissage appliqué aux dérivées d'angle (précession, inclinaison). "
        "0 = brut. Une dérivée non lissée à 100 Hz est dominée par la "
        "quantification du capteur.",
)


def _smoothed_rate(ctx, key: str, current, previous_key: str,
                   *, angular: bool):
    """
    Shared body of every "rate of change of an angle" signal.

    Angular differences go through the wrap-safe delta: without it a wheel
    crossing 360° produces a −36000 °/s spike that any threshold would happily
    report as a violent event.
    """
    state = ctx.state
    prev  = state.get(previous_key)
    state[previous_key] = current

    if current is None or prev is None or not ctx.integrable:
        return state.get(key)

    delta = angle_delta_deg(current, prev) if angular else (current - prev)
    rate  = delta / ctx.dt

    smoothed = state.get(key)
    tau = ctx.param(P_RATE_TAU)
    if smoothed is None or tau <= 0:
        smoothed = rate
    else:
        smoothed += ctx.alpha(tau) * (rate - smoothed)
    state[key] = smoothed
    return smoothed


# ── Position of the centre ───────────────────────────────────────────────────

@signal(
    "pos_x", kind=DYNAMIC, unit="m", range=(-20, 20),
    needs=(ATTITUDE_REL, OMEGA),
    doc="Position du centre selon x, intégrée sous contrainte de roulement "
        "sans glissement. Dérive : c'est une intégrale sans référence absolue.",
)
def pos_x(ctx):
    return _integrate(ctx)[0]


@signal(
    "pos_y", kind=DYNAMIC, unit="m", range=(-20, 20),
    needs=(ATTITUDE_REL, OMEGA), after=("pos_x",),
    doc="Position du centre selon y. Même dérive que pos_x.",
)
def pos_y(ctx):
    return _integrate(ctx)[1]


@signal(
    "pos_z", kind=DYNAMIC, unit="m", range=(0, 1.2),
    needs=(ATTITUDE_REL,),
    doc="Hauteur du centre. Identique à height_m : forme close, sans dérive — "
        "exposée ici pour que pos_x/y/z forment un triplet utilisable tel quel.",
)
def pos_z(ctx):
    k = kinematics(ctx)
    return None if k is None else k.height


def _integrate(ctx):
    """
    Euler-integrate the centre's horizontal position, once per tick.

    Shared between pos_x and pos_y through the tick scratch: integrating twice
    would advance the state twice and double the speed.
    """
    done = ctx.scratch.get("position")
    if done is not None:
        return done

    # The integrator's own state has to live somewhere stable, and ctx.state is
    # keyed on the running signal — so it is anchored on pos_x, which `after`
    # guarantees runs first.
    state = ctx._states.setdefault("pos_x", {})
    px = state.get("px", 0.0)
    py = state.get("py", 0.0)

    k = kinematics(ctx)
    if k is not None and k.p_dot is not None and ctx.integrable:
        px += float(k.p_dot[0]) * ctx.dt
        py += float(k.p_dot[1]) * ctx.dt
        state["px"], state["py"] = px, py

    ctx.scratch["position"] = (px, py)
    return px, py


def seed_position(model, x: float, y: float) -> None:
    """
    Plant the integrator where a take says the wheel was.

    Every other value in the model forgets its past in ~5 τ, so a few seconds
    of re-feeding recovers it; the horizontal position is a path integral and
    never comes back (ADR 0004).  Without this, resuming a take at a chosen
    instant teleports the wheel between the position read off the pose track
    and the one the integrator happens to have landed on.

    It lives here rather than in the seek code because *where* px/py are kept is
    `_integrate`'s business — the two are three lines apart precisely so they
    cannot drift.  Call it after the warm-up, never before: the run that
    follows would integrate away from the seeded point instead of towards it.
    """
    model.node_state("pos_x").update(px=float(x), py=float(y))


# ── Speed and heading ────────────────────────────────────────────────────────

@signal(
    "speed_ms", kind=DYNAMIC, unit="m/s", range=(0, 8),
    needs=(ATTITUDE_REL, OMEGA),
    doc="Vitesse horizontale du centre, déduite de la contrainte de roulement. "
        "Instantanée et sans dérive, contrairement à la position qu'elle intègre.",
)
def speed_ms(ctx):
    k = kinematics(ctx)
    if k is None or k.p_dot is None:
        return None
    return float(math.hypot(k.p_dot[0], k.p_dot[1]))


@signal(
    "heading_deg", kind=DYNAMIC, unit="deg", range=(0, 360),
    needs=(ATTITUDE_REL, OMEGA), depends=("speed_ms",),
    params=(P_MIN_SPEED_HEADING,),
    doc="Cap de déplacement du centre, repère relatif. Gelé sous la vitesse "
        "minimale, où la direction n'est plus que du bruit.",
)
def heading_deg(ctx):
    speed = ctx["speed_ms"]
    held  = ctx.state.get("held")
    if speed is None or speed < ctx.param(P_MIN_SPEED_HEADING):
        return held

    k = kinematics(ctx)
    if k is None or k.p_dot is None:
        return held

    value = heading_of(float(k.p_dot[0]), float(k.p_dot[1]))
    ctx.state["held"] = value
    return value


# ── Angular rates ────────────────────────────────────────────────────────────

@signal(
    "spin_rate_dps", kind=DYNAMIC, unit="°/s", range=(-720, 720),
    needs=(ATTITUDE_REL, OMEGA),
    doc="Vitesse de rotation propre autour de l'axe de la roue — la vitesse à "
        "laquelle elle roule. Lue directement sur le gyroscope, sans dérivation.",
)
def spin_rate_dps(ctx):
    omega = ctx.vec(OMEGA)
    R = ctx.matrix(ATTITUDE_REL)
    if omega is None or R is None:
        return None
    # The axle is local z, so the spin rate is simply ω's z component in the
    # sensor frame — no differentiation, hence no noise amplification.
    return math.degrees(float(omega[2]))


@signal(
    "precession_rate_dps", kind=DYNAMIC, unit="°/s", range=(-360, 360),
    needs=(ATTITUDE_REL,), depends=("tilt_dir_deg",), params=(P_RATE_TAU,),
    doc="Vitesse à laquelle la direction d'inclinaison tourne — la précession. "
        "C'est ce qui distingue une valse d'une ligne droite.",
)
def precession_rate_dps(ctx):
    return _smoothed_rate(ctx, "rate", ctx["tilt_dir_deg"], "prev", angular=True)


@signal(
    "lean_rate_dps", kind=DYNAMIC, unit="°/s", range=(-360, 360),
    needs=(ATTITUDE_REL,), depends=("lean_deg",), params=(P_RATE_TAU,),
    doc="Vitesse de variation de l'inclinaison : la roue se couche ou se "
        "redresse. Positif = elle se couche.",
)
def lean_rate_dps(ctx):
    return _smoothed_rate(ctx, "rate", ctx["lean_deg"], "prev", angular=False)


@signal(
    "omega_norm_dps", kind=DYNAMIC, unit="°/s", range=(0, 900),
    needs=(OMEGA,),
    doc="Norme de la vitesse angulaire, toutes composantes confondues.",
)
def omega_norm_dps(ctx):
    omega = ctx.vec(OMEGA)
    if omega is None:
        return None
    return math.degrees(float(math.sqrt(sum(float(v) ** 2 for v in omega))))


# ── Intensity of the movement ────────────────────────────────────────────────

@signal(
    "motion_ms", kind=DYNAMIC, unit="m/s", range=(0, 12),
    needs=(ATTITUDE_REL, OMEGA), depends=("speed_ms", "omega_norm_dps"),
    doc="Intensité globale du mouvement, exprimée en vitesse équivalente : "
        "translation du centre et rotation de la jante combinées. La grandeur "
        "à mapper quand on veut « à quel point ça bouge ».",
)
def motion_ms(ctx):
    speed = ctx["speed_ms"]
    omega = ctx["omega_norm_dps"]
    if speed is None or omega is None:
        return None
    k = kinematics(ctx)
    if k is None:
        return None
    # Rim speed for the rotational part, so both terms are metres per second and
    # can be compared and summed without an arbitrary weighting.
    rim = math.radians(omega) * k.wheel_R
    return math.hypot(speed, rim)


@signal(
    "motion_fast", kind=DYNAMIC, unit="m/s", range=(0, 12),
    needs=(ATTITUDE_REL, OMEGA), depends=("motion_ms",), params=(P_MOTION_TAU_FAST,),
    doc="Enveloppe rapide de l'intensité : suit le geste.",
)
def motion_fast(ctx):
    return _envelope(ctx, ctx["motion_ms"], ctx.param(P_MOTION_TAU_FAST))


@signal(
    "motion_slow", kind=DYNAMIC, unit="m/s", range=(0, 12),
    needs=(ATTITUDE_REL, OMEGA), depends=("motion_ms",), params=(P_MOTION_TAU_SLOW,),
    doc="Enveloppe lente de l'intensité : le niveau général de la séquence.",
)
def motion_slow(ctx):
    return _envelope(ctx, ctx["motion_ms"], ctx.param(P_MOTION_TAU_SLOW))


@signal(
    "motion_burst", kind=DYNAMIC, unit="", range=(0, 4),
    needs=(ATTITUDE_REL, OMEGA), depends=("motion_fast", "motion_slow"),
    doc="Rapport enveloppe rapide / enveloppe lente : « il se passe quelque "
        "chose maintenant », indépendamment du niveau absolu. Vaut 1 au repos "
        "comme en plein roulage régulier, et monte sur une accélération.",
)
def motion_burst(ctx):
    fast, slow = ctx["motion_fast"], ctx["motion_slow"]
    if fast is None or slow is None or slow < 1e-6:
        return None
    return fast / slow


def _envelope(ctx, value, tau: float):
    """One-pole envelope, rate-independent by construction."""
    if value is None:
        return ctx.state.get("env")
    env = ctx.state.get("env")
    env = value if env is None else env + ctx.alpha(tau) * (value - env)
    ctx.state["env"] = env
    return env


# ── Acceleration (only when the accelerometer is configured) ─────────────────

@signal(
    "accel_norm_ms2", kind=DYNAMIC, unit="m/s²", range=(0, 60),
    needs=(ACCEL,),
    doc="Norme de l'accélération mesurée, gravité comprise. Vaut ~9,81 au "
        "repos. C'est la grandeur sur laquelle se détectent les chocs.",
)
def accel_norm_ms2(ctx):
    a = ctx.vec(ACCEL)
    if a is None:
        return None
    return float(math.sqrt(sum(float(v) ** 2 for v in a)))


@signal(
    "accel_shock_ms2", kind=DYNAMIC, unit="m/s²", range=(0, 40),
    needs=(ACCEL,), depends=("accel_norm_ms2",), params=(P_RATE_TAU,),
    doc="Écart de l'accélération à sa propre moyenne glissante : la gravité et "
        "le régime établi s'annulent, seuls restent les transitoires. C'est "
        "l'entrée naturelle d'un détecteur de choc.",
)
def accel_shock_ms2(ctx):
    value = ctx["accel_norm_ms2"]
    if value is None:
        return None
    baseline = ctx.state.get("baseline")
    if baseline is None:
        ctx.state["baseline"] = value
        return 0.0
    # Deliberately a longer constant than the derivative smoothing: the baseline
    # must follow posture changes without following the shock it is meant to
    # reveal.
    baseline += ctx.alpha(0.5) * (value - baseline)
    ctx.state["baseline"] = baseline
    return abs(value - baseline)
