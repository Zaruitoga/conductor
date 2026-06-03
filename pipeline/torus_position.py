"""
pipeline/torus_position.py — 3D torus centre position computation.

Expected data source:

  This stage consumes SUPER_0 packets (typeId=0x10), i.e. super slot 0
  configured on the ESP32 as:
    deps = [slot 0 (GYRO), slot 6 (GAME_RV)],  skip_ratio = 1

  With that configuration the parser emits:
    gyro_x, gyro_y, gyro_z          (GYRO,    3 floats, rad/s)
    game_rv_qw, game_rv_qx,
    game_rv_qy, game_rv_qz           (GAME_RV, 4 floats, unit quaternion)

  If the super slot is not configured, no 0x10 packets will arrive and all
  other packet types are forwarded unmodified.

Physics:

  Phase 2 — Pz (closed-form, drift-free):
    Pz = R_tore * u_perp + r_tore
    where u_perp is the horizontal component of the wheel-plane normal.

  Phase 3 — Px, Py (Euler integration, no-slip constraint):
    r_contact = world-frame vector from centre to contact point
    Ṗ = -(R·ω_local) × r_contact   →   integrated with dt from ts_esp_us
"""

import numpy as np
from scipy.spatial.transform import Rotation
import logging

from .base import PipelineStage
from config import R_TORE, r_TORE, DEGENERATE_THRESHOLD

log = logging.getLogger("torus_position")

# Required packet fields for this stage
_REQUIRED_FIELDS = (
    "gyro_x", "gyro_y", "gyro_z",
    "game_rv_qw", "game_rv_qx", "game_rv_qy", "game_rv_qz",
)


class TorusPositionStage(PipelineStage):
    """
    Computes the 3D position of the torus centre from GYRO + GAME_RV data.

    Input:  SUPER_0 packets (typeId=0x10) with named fields from deps=[GYRO, GAME_RV].
    Output: same packet enriched with px, py, pz and typeId=5 ("computed").
    """

    SOURCE_TYPE_ID = 0x10

    def __init__(self):
        self._last_ts_esp_us: int | None = None
        self._px: float = 0.0
        self._py: float = 0.0

    async def process(self, packet: dict) -> dict | None:
        if packet.get("typeId") != self.SOURCE_TYPE_ID:
            return packet   # pass through all other packet types unchanged

        if not all(k in packet for k in _REQUIRED_FIELDS):
            missing = [k for k in _REQUIRED_FIELDS if k not in packet]
            log.warning(
                f"SUPER_0 missing fields {missing} — "
                "check ESP config (deps=[GYRO, GAME_RV])"
            )
            return None

        dt = self._compute_dt(packet["ts_esp_us"])
        if dt is None or dt <= 0:
            return None     # first packet or inconsistent dt — drop

        omega_local = [packet["gyro_x"],    packet["gyro_y"],    packet["gyro_z"]]
        # scipy quaternion convention: [qx, qy, qz, qw]
        q           = [packet["game_rv_qx"], packet["game_rv_qy"],
                       packet["game_rv_qz"], packet["game_rv_qw"]]

        px, py, pz = self._compute_position(q, omega_local, dt)

        return {
            **packet,
            "typeId": 5,
            "type":   "computed",
            "px": px,
            "py": py,
            "pz": pz,
        }

    async def reset(self) -> None:
        """Reset integration state for a new session."""
        self._last_ts_esp_us = None
        self._px = 0.0
        self._py = 0.0
        log.info("TorusPositionStage reset")

    def _compute_dt(self, ts_esp_us: int) -> float | None:
        """Return elapsed time in seconds since the last packet, or None on first call."""
        if self._last_ts_esp_us is None:
            self._last_ts_esp_us = ts_esp_us
            return None
        dt = (ts_esp_us - self._last_ts_esp_us) / 1e6
        self._last_ts_esp_us = ts_esp_us
        return dt

    def _compute_position(
        self,
        q: list[float],           # [qx, qy, qz, qw] scipy convention
        omega_local: list[float], # [gx, gy, gz] rad/s in IMU frame
        dt: float,
    ) -> tuple[float, float, float]:
        """
        Compute (px, py, pz) using the no-slip rolling constraint.

        Pz is computed analytically; Px and Py are Euler-integrated.
        """
        rot   = Rotation.from_quat(q)
        R_mat = rot.as_matrix()

        # World-frame "up" vector expressed in the local frame
        u      = R_mat.T @ np.array([0.0, 0.0, 1.0])
        u_perp = np.sqrt(u[0]**2 + u[1]**2)

        pz = R_TORE * u_perp + r_TORE

        if u_perp < DEGENERATE_THRESHOLD:
            # Wheel is flat — horizontal displacement is undefined
            return self._px, self._py, pz

        # World-frame vector from torus centre to contact point
        scale     = (R_TORE + r_TORE * u_perp) / u_perp
        r_contact = R_mat @ np.array([
            -scale * u[0],
            -scale * u[1],
            -r_TORE * u[2],
        ])

        # Angular velocity in world frame
        omega_world = R_mat @ np.array(omega_local)

        # Centre velocity from no-slip constraint (X and Y components)
        p_dot = -np.cross(omega_world, r_contact)

        self._px += p_dot[0] * dt
        self._py += p_dot[1] * dt

        return self._px, self._py, pz
