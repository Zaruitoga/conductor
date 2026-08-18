"""
storage/csv_logger.py — Logs all received UDP packets to a CSV file.

Runs before the pipeline (see main.py) to preserve raw data independently
of the computation model.

CSV schema — one quantity, one column:

  Common columns (always present):
    ts_rx_us, seq, ts_esp_us, type_id

  Payload columns (blank when the packet does not carry them):
    gyro_x/y/z, accel_x/y/z, mag_x/y/z, linear_accel_x/y/z,
    rv_qw/qx/qy/qz, geo_rv_qw/qx/qy/qz,
    game_rv_qw/qx/qy/qz, arvr_rv_qw/qx/qy/qz

A gyro lands in gyro_x/y/z whether it arrived as its own 0x01 packet or inside
a super slot — the column says what it holds, and a tool reading a take never
has to know how the ESP was wired to find a quantity (issue #12).  There is no
table here to keep in step with the reader's: both sides name columns from
`protocol.PACKET_FIELDS`, and a packet's keys *are* its columns.  The anonymous
x/y/z and qw/qx/qy/qz columns are retired; the four takes recorded with them
were migrated when the change landed, and no reader tolerates them.

`type_id` stays, and is now the only thing separating a simple row from a super
one.  It is not a decoding aid but the row's record of which datagram it was:
`row_to_packet` rebuilds the packet's type from it, and that name is what
LiveMonitor counts per-type rates under and what a WebSocket client filters on
with `?types=`.

Heartbeat packets (typeId 0x20) are telemetry, not sensor data, and are
deliberately NOT logged — they are absent from PACKET_FIELDS, so write() skips
them for the same reason it skips a CFG_ACK.

Note: super packets received before the layout is populated (s{i} fallback
mode) are not stored — a debug warning is emitted by the parser.
"""

import csv
import logging

from storage.session_manager import SessionManager, TakeMeta
from transport.protocol import ALL_SUPER_NAMED_FIELDS, PACKET_FIELDS

log = logging.getLogger("csv_logger")

# Every payload column there is, in slot order. A simple slot's fields are a
# subset of these, so the schema is the super set and nothing else — 32 columns,
# down from 39 (issue #12 retired the seven anonymous ones).
CSV_FIELDS = [
    "ts_rx_us", "seq", "ts_esp_us", "type_id",
    *ALL_SUPER_NAMED_FIELDS,
]


class CSVLogger:
    """Writes one CSV row per packet to the active take's file."""

    def __init__(self, session_manager: SessionManager):
        self._sm       = session_manager
        self._file     = None
        self._writer   = None
        self._take_dir: str | None      = None
        self._meta:     TakeMeta | None = None
        self.active     = False

    def start(self, take_dir: str, meta: TakeMeta) -> None:
        """Open the take's CSV file and write the header row."""
        if self.active:
            log.warning("Logger already active — call stop() first")
            return

        self._take_dir = take_dir
        self._meta     = meta
        csv_path       = self._sm.csv_path(take_dir)

        self._file   = open(csv_path, "w", newline="")
        self._writer = csv.DictWriter(
            self._file, fieldnames=CSV_FIELDS, extrasaction="ignore"
        )
        self._writer.writeheader()
        self.active = True
        log.info(f"Recording started → {csv_path}")

    def stop(self) -> None:
        """Flush, close the file, and finalise take metadata."""
        if not self.active:
            return
        self.active = False
        self._file.flush()
        self._file.close()
        self._file   = None
        self._writer = None
        self._sm.close_take(self._take_dir, self._meta)
        log.info(
            f"Recording stopped — {self._meta.packet_count} packets "
            f"in {self._take_dir}"
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
        if type_id not in PACKET_FIELDS:
            return   # heartbeat, CFG_ACK, unknown types, etc.

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
        for field in PACKET_FIELDS[type_id]:
            v = packet.get(field)
            row[field] = "" if v is None else v

        self._writer.writerow(row)

        m = self._meta
        m.packet_count += 1
        if m.first_ts_rx_us == 0:
            m.first_ts_rx_us = packet["ts_rx_us"]
        m.last_ts_rx_us = packet["ts_rx_us"]
