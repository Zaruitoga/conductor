"""
storage/csv_logger.py — Logs all received UDP packets to a CSV file.

Runs before the pipeline (see main.py) to preserve raw data independently
of the computation model.

CSV schema:

  Common columns (always present):
    ts_rx_us, seq, ts_esp_us, type_id

  Simple sensor columns (blank when not applicable):
    x, y, z              Vec3   (typeId 0x01–0x04)
    qw, qx, qy, qz       Quat   (typeId 0x05–0x08)
    percent              BAT    (typeId 0x20)

  Super-slot columns (typeId 0x10–0x17):
    Named by component sensor — only the fields corresponding to the active
    deps of that super slot are populated; the rest are blank.
    Full column list (slot order):
      gyro_x/y/z, accel_x/y/z, mag_x/y/z, linear_accel_x/y/z,
      rv_qw/qx/qy/qz, geo_rv_qw/qx/qy/qz,
      game_rv_qw/qx/qy/qz, arvr_rv_qw/qx/qy/qz

Note: super packets received before the layout is populated (s{i} fallback
mode) are not stored — a debug warning is emitted by the parser.
"""

import csv
import logging

from storage.session_manager import SessionManager, SessionMeta
from transport.super_layout import ALL_SUPER_NAMED_FIELDS

log = logging.getLogger("csv_logger")

_VEC3_FIELDS = ("x", "y", "z")
_QUAT_FIELDS = ("qw", "qx", "qy", "qz")
_BAT_FIELDS  = ("percent",)

CSV_FIELDS = [
    "ts_rx_us", "seq", "ts_esp_us", "type_id",
    *_VEC3_FIELDS,
    *_QUAT_FIELDS,
    *_BAT_FIELDS,
    *ALL_SUPER_NAMED_FIELDS,
]

# Payload columns to extract, keyed by typeId
PAYLOAD_FIELDS: dict[int, tuple[str, ...]] = {
    0x01: _VEC3_FIELDS,
    0x02: _VEC3_FIELDS,
    0x03: _VEC3_FIELDS,
    0x04: _VEC3_FIELDS,
    0x05: _QUAT_FIELDS,
    0x06: _QUAT_FIELDS,
    0x07: _QUAT_FIELDS,
    0x08: _QUAT_FIELDS,
    0x20: _BAT_FIELDS,
    # All super types share the full named-field set; only populated fields
    # will have values — the rest are left blank by the write() method.
    **{0x10 + i: ALL_SUPER_NAMED_FIELDS for i in range(8)},
}


class CSVLogger:
    """Writes one CSV row per packet to the active session file."""

    def __init__(self, session_manager: SessionManager):
        self._sm          = session_manager
        self._file        = None
        self._writer      = None
        self._session_dir: str | None        = None
        self._meta:        SessionMeta | None = None
        self.active        = False

    def start(self, session_dir: str, meta: SessionMeta) -> None:
        """Open the CSV file and write the header row."""
        if self.active:
            log.warning("Logger already active — call stop() first")
            return

        self._session_dir = session_dir
        self._meta        = meta
        csv_path          = self._sm.csv_path(session_dir)

        self._file   = open(csv_path, "w", newline="")
        self._writer = csv.DictWriter(
            self._file, fieldnames=CSV_FIELDS, extrasaction="ignore"
        )
        self._writer.writeheader()
        self.active = True
        log.info(f"Recording started → {csv_path}")

    def stop(self) -> None:
        """Flush, close the file, and finalise session metadata."""
        if not self.active:
            return
        self.active = False
        self._file.flush()
        self._file.close()
        self._file   = None
        self._writer = None
        self._sm.close_session(self._session_dir, self._meta)
        log.info(
            f"Recording stopped — {self._meta.packet_count} packets "
            f"in {self._session_dir}"
        )

    def write(self, packet: dict) -> None:
        """
        Write one CSV row for a packet of known type.

        For super-slot packets, only fields that exist in the packet are
        written (i.e. the fields for the active deps); all other named super
        columns are left blank.  Packets still using the s{i} fallback
        (dep_slots=None) are silently skipped.
        """
        if not self.active:
            return

        type_id = packet.get("typeId")
        if type_id not in PAYLOAD_FIELDS:
            return   # CFG_ACK, unknown types, etc.

        # Skip super packets that arrived before the layout was known
        if 0x10 <= type_id <= 0x17 and packet.get("dep_slots") is None:
            log.debug(
                f"Super packet 0x{type_id:02X} skipped (layout not yet known)"
            )
            return

        row: dict = {
            "ts_rx_us": packet.get("ts_rx_us", ""),
            "seq":       packet.get("seq", ""),
            "ts_esp_us": packet.get("ts_esp_us", ""),
            "type_id":   type_id,
        }
        for field in PAYLOAD_FIELDS[type_id]:
            v = packet.get(field)
            row[field] = "" if v is None else v

        self._writer.writerow(row)

        m = self._meta
        m.packet_count += 1
        if m.first_ts_rx_us == 0:
            m.first_ts_rx_us = packet["ts_rx_us"]
        m.last_ts_rx_us = packet["ts_rx_us"]

    def mark_sync(self, ts_rx_us: int) -> None:
        """Record a video sync marker (clap) timestamp in the session sidecar."""
        if not self.active:
            log.warning("Sync marker ignored: no active recording")
            return
        self._sm.set_sync_marker(self._session_dir, self._meta, ts_rx_us)
        log.info(f"Sync marker recorded at ts_rx_us={ts_rx_us}")
