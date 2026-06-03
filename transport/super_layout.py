"""
transport/super_layout.py — Shared super-slot field-name registry.

SuperSlotLayout is a lightweight shared-state object that maps super slot
indices to their component simple slot list.  It is written by
EspConfigurator (on every ACK) and read by parse_packet (on every super
datagram) so that the parser can emit properly named payload fields instead
of the opaque s0..sN fallback.

Field naming follows the firmware's SLOT_DEF order (report_manager.cpp):
  slot 0  GYRO          → gyro_x,           gyro_y,           gyro_z
  slot 1  ACCEL         → accel_x,          accel_y,          accel_z
  slot 2  MAG           → mag_x,            mag_y,            mag_z
  slot 3  LINEAR_ACCEL  → linear_accel_x,   linear_accel_y,   linear_accel_z
  slot 4  RV            → rv_qw,            rv_qx,            rv_qy,    rv_qz
  slot 5  GEO_RV        → geo_rv_qw,        geo_rv_qx,        geo_rv_qy, geo_rv_qz
  slot 6  GAME_RV       → game_rv_qw,       game_rv_qx,       game_rv_qy, game_rv_qz
  slot 7  ARVR_RV       → arvr_rv_qw,       arvr_rv_qx,       arvr_rv_qy, arvr_rv_qz
"""

# Payload field names for each simple slot
SLOT_FIELDS: dict[int, tuple[str, ...]] = {
    0: ("gyro_x",           "gyro_y",           "gyro_z"),
    1: ("accel_x",          "accel_y",          "accel_z"),
    2: ("mag_x",            "mag_y",            "mag_z"),
    3: ("linear_accel_x",   "linear_accel_y",   "linear_accel_z"),
    4: ("rv_qw",            "rv_qx",            "rv_qy",            "rv_qz"),
    5: ("geo_rv_qw",        "geo_rv_qx",        "geo_rv_qy",        "geo_rv_qz"),
    6: ("game_rv_qw",       "game_rv_qx",       "game_rv_qy",       "game_rv_qz"),
    7: ("arvr_rv_qw",       "arvr_rv_qx",       "arvr_rv_qy",       "arvr_rv_qz"),
}

# Number of floats in each simple slot's payload (Vec3 = 3, Quat = 4)
SLOT_FLOAT_COUNT: dict[int, int] = {
    0: 3, 1: 3, 2: 3, 3: 3,
    4: 4, 5: 4, 6: 4, 7: 4,
}

# All possible named super-payload fields, enumerated in slot order.
# Used by csv_logger and playback_engine as the fixed CSV column set for super rows.
ALL_SUPER_NAMED_FIELDS: tuple[str, ...] = sum(
    (SLOT_FIELDS[i] for i in range(8)), ()
)


class SuperSlotLayout:
    """
    Maps super slot indices to their component simple slot list.

    Written by EspConfigurator on every ACK, read by parse_packet on every
    super-slot datagram.  Dict operations are protected by the GIL, which is
    sufficient given that the writer runs in a ThreadPoolExecutor thread and
    the reader runs in the asyncio event loop thread.

    Until the first ACK is received, all get_deps() calls return None and
    parse_packet falls back to generic s{i} field naming.
    """

    def __init__(self):
        self._deps: dict[int, list[int]] = {}

    def update(self, state: dict) -> None:
        """Synchronise from a parsed ACK state dict."""
        for s in state.get("supers", []):
            if s["active"]:
                self._deps[s["slot"]] = list(s["deps"])
            else:
                self._deps.pop(s["slot"], None)

    def get_deps(self, super_idx: int) -> list[int] | None:
        """Return the dep_slots list for a super slot, or None if not yet known."""
        return self._deps.get(super_idx)
