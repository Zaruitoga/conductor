"""
simulator/motion.py — Kinematic model of a rolling Cyr wheel.

Produces the sensor values the fake IMU reports, from an attitude prescribed
analytically as a function of time.

Frame convention (imposed by the pipeline, see pipeline/torus_position.py):
    the stage computes u = Rᵀ·[0,0,1] and pz = R_TORE·u_perp + r_TORE, so an
    upright wheel must give u_perp = 1.  That means world-up lies *in* the
    local xy-plane: the **wheel plane is the local xy-plane** and the **axle is
    local z**.

Attitude, in a 3-1-3 Euler sequence:

    R(t) = Rz(ψ) · Rx(90° + λ) · Rz(φ)

    ψ  precession about the world vertical
    λ  lean away from vertical (λ = 0 ⇒ upright wheel)
    φ  spin about the wheel's own axle

which yields u = [sin φ·cos λ, cos φ·cos λ, −sin λ], hence u_perp = cos λ and

    pz = R_TORE·cos λ + r_TORE                            (closed form)

The gyro is **differentiated numerically from that same attitude** rather than
prescribed independently, so ω_local and the emitted quaternion are consistent
by construction: any discrepancy observed downstream comes from the transport
or the pipeline, never from the data itself.

Ground truth (`reference`) is only claimed where it is genuinely analytic and
independent of the pipeline's own formula:
    pz        — every scenario (geometry above)
    px, py    — `static` and `straight` only; None elsewhere, where the useful
                check is qualitative instead (a `coin` trajectory must close
                into a circle of period 2π/ψ̇).
"""

import math

import numpy as np
from scipy.spatial.transform import Rotation

from config import R_TORE, r_TORE

SCENARIOS = ("static", "straight", "coin", "spiral")

_GRAVITY_MS2 = 9.80665
# Rough mid-latitude geomagnetic field in the world frame (µT).
_MAG_WORLD   = np.array([20.0, 0.0, 43.0])
# Half-step of the central difference used to derive the gyro (seconds).
_GYRO_H      = 1e-4
# Radius the wheel actually rolls on: the contact point sits R + r from the
# centre, so a straight roll advances at (R_TORE + r_TORE)·φ̇.
_ROLL_RADIUS = R_TORE + r_TORE


class WheelMotion:
    """
    Analytic attitude of a Cyr wheel, plus the IMU readings that follow from it.

    All methods take `t`, the time in seconds since the simulated boot.
    """

    def __init__(
        self,
        scenario:        str   = "coin",
        lean_deg:        float = 20.0,
        spin_dps:        float = 180.0,
        precession_dps:  float = 45.0,
        spiral_period_s: float = 20.0,
    ):
        if scenario not in SCENARIOS:
            raise ValueError(f"Unknown scenario {scenario!r}, expected one of {SCENARIOS}")
        self.scenario   = scenario
        self._lean      = math.radians(lean_deg)
        self._spin      = math.radians(spin_dps)
        self._precess   = math.radians(precession_dps)
        self._spiral_T  = spiral_period_s

    # ── Prescribed attitude ──────────────────────────────────────────────────

    def _angles(self, t: float) -> tuple[float, float, float]:
        """Return (ψ, λ, φ) in radians for the active scenario."""
        if self.scenario == "static":
            return 0.0, 0.0, 0.0

        if self.scenario == "straight":
            # Upright, no precession: the centre must travel in a straight line.
            return 0.0, 0.0, self._spin * t

        if self.scenario == "coin":
            # The classic rolling coin: constant lean, steady precession.
            return self._precess * t, self._lean, self._spin * t

        # spiral — lean ramps up from 0, so the run also crosses the
        # near-degenerate region the pipeline guards with DEGENERATE_THRESHOLD.
        ramp = 0.5 - 0.5 * math.cos(2 * math.pi * t / self._spiral_T)
        return self._precess * t, self._lean * ramp, self._spin * t

    def attitude(self, t: float) -> Rotation:
        """Wheel orientation as a scipy Rotation (local → world)."""
        psi, lean, phi = self._angles(t)
        return Rotation.from_euler(
            "ZXZ", [psi, math.pi / 2 + lean, phi]
        )

    # ── Sensor readings ──────────────────────────────────────────────────────

    def quaternion(self, t: float) -> tuple[float, float, float, float]:
        """Attitude as (qw, qx, qy, qz) — the wire order for Quat packets."""
        qx, qy, qz, qw = self.attitude(t).as_quat()   # scipy order is x y z w
        return float(qw), float(qx), float(qy), float(qz)

    def gyro(self, t: float) -> tuple[float, float, float]:
        """
        Body-frame angular velocity (rad/s), central-differenced from attitude.

        rotvec(R(t−h)ᵀ·R(t+h)) / 2h is the body-frame ω, so the gyro can never
        contradict the quaternion stream.
        """
        r0 = self.attitude(t - _GYRO_H)
        r1 = self.attitude(t + _GYRO_H)
        omega = (r0.inv() * r1).as_rotvec() / (2 * _GYRO_H)
        return tuple(float(v) for v in omega)

    def accel(self, t: float) -> tuple[float, float, float]:
        """Specific force in the body frame — here just gravity (m/s²)."""
        up_local = self.attitude(t).as_matrix().T @ np.array([0.0, 0.0, 1.0])
        return tuple(float(v) for v in up_local * _GRAVITY_MS2)

    def mag(self, t: float) -> tuple[float, float, float]:
        """Geomagnetic field rotated into the body frame (µT)."""
        local = self.attitude(t).as_matrix().T @ _MAG_WORLD
        return tuple(float(v) for v in local)

    def linear_accel(self, t: float) -> tuple[float, float, float]:
        """
        Gravity-free acceleration.

        The model prescribes attitude, not centre dynamics, so this stays zero;
        it exists so slot 3 emits something well-formed when enabled.
        """
        return 0.0, 0.0, 0.0

    # ── Ground truth ─────────────────────────────────────────────────────────

    def reference(self, t: float) -> dict:
        """
        Analytically-known centre position at time `t`.

        `px`/`py` are None for scenarios with no closed form (see module
        docstring); `pz` is always exact.
        """
        _, lean, _ = self._angles(t)
        pz = R_TORE * math.cos(lean) + r_TORE

        if self.scenario == "static":
            return {"px": 0.0, "py": 0.0, "pz": pz}

        if self.scenario == "straight":
            # Rolling upright along −x at (R_TORE + r_TORE)·φ̇ (see module docstring).
            return {"px": -_ROLL_RADIUS * self._spin * t, "py": 0.0, "pz": pz}

        return {"px": None, "py": None, "pz": pz}

    def reference_note(self) -> str:
        """One-line description of what the trajectory should look like."""
        if self.scenario == "static":
            return f"immobile, pz = {R_TORE + r_TORE:.3f} m"
        if self.scenario == "straight":
            speed = _ROLL_RADIUS * self._spin
            return (f"droite le long de −x à {speed:.3f} m/s, "
                    f"pz = {R_TORE + r_TORE:.3f} m")
        if self.scenario == "coin":
            pz = R_TORE * math.cos(self._lean) + r_TORE
            period = (2 * math.pi / self._precess) if self._precess else float("inf")
            return (f"cercle fermé de période {period:.1f} s, "
                    f"pz constant = {pz:.3f} m")
        return f"rayon variable, pz oscillant entre {R_TORE * math.cos(self._lean) + r_TORE:.3f} et {R_TORE + r_TORE:.3f} m"
