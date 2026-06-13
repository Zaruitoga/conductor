"""
storage/playback_engine.py — CSV session replay engine.

Replays a recorded session by pushing packets onto the central Queue.
The downstream pipeline and WebSocket server see no difference from live data.

Timing: original ts_esp_us intervals are preserved.
The `speed` factor allows faster or slower playback.

End signal:
  When the CSV is exhausted, a sentinel packet {"typeId": "playback_end"} is
  pushed onto the Queue so main.py can transition back to IDLE mode.

Note on super-slot packets:
  During playback, packets are reconstructed from the named fields stored in
  the CSV (gyro_x, game_rv_qw, etc.).  The dep_slots field is not included
  in playback packets since it is not stored in the CSV; downstream stages
  that need it should use the field names directly.
"""

import asyncio
import csv
import logging
import os

from storage.session_manager import SessionManager
from transport.super_layout import ALL_SUPER_NAMED_FIELDS

log = logging.getLogger("playback_engine")

# Must stay in sync with udp_receiver.py
PACKET_TYPES: dict[int, str] = {
    0x01: "gyro",         0x02: "accel",    0x03: "mag",
    0x04: "linear_accel", 0x05: "rv",       0x06: "geo_rv",
    0x07: "game_rv",      0x08: "arvr_rv",  0x20: "battery",
}
for _i in range(8):
    PACKET_TYPES[0x10 + _i] = f"super_{_i}"

_VEC3_FIELDS = ("x", "y", "z")
_QUAT_FIELDS = ("qw", "qx", "qy", "qz")
_BAT_FIELDS  = ("percent",)

PAYLOAD_FIELDS: dict[int, tuple[str, ...]] = {
    0x01: _VEC3_FIELDS,  0x02: _VEC3_FIELDS,
    0x03: _VEC3_FIELDS,  0x04: _VEC3_FIELDS,
    0x05: _QUAT_FIELDS,  0x06: _QUAT_FIELDS,
    0x07: _QUAT_FIELDS,  0x08: _QUAT_FIELDS,
    0x20: _BAT_FIELDS,
    # Super types: scan all named fields; only populated columns are loaded
    **{0x10 + i: ALL_SUPER_NAMED_FIELDS for i in range(8)},
}

SENTINEL = {"typeId": "playback_end"}


class PlaybackEngine:
    """Replays a recorded CSV session as a stream of packets."""

    def __init__(self, session_manager: SessionManager):
        self._sm    = session_manager
        self._task: asyncio.Task | None = None
        self.active = False

        # Progress, exposed via GET /api/playback/status.
        self.session:   str | None = None
        self.take:      str | None = None
        self.speed:     float      = 1.0
        self.loop:      bool       = False
        self.index:     int        = 0     # current row (1-based once playing)
        self.total:     int        = 0     # total rows in the take
        self.elapsed_s: float      = 0.0   # take time reached
        self.total_s:   float      = 0.0   # take duration

    async def start(
        self,
        session:         str,
        take:            str,
        queue:           asyncio.Queue,
        pipeline_stages: list,
        speed:           float = 1.0,
        loop:            bool  = False,
    ) -> None:
        """
        Start replaying a take in the background.

        Resets all pipeline stages before the first packet is pushed so that
        integrators and other stateful stages start from a clean state.
        With loop=True the take restarts (and stages reset) on each pass.
        """
        if self.active:
            log.warning("Playback already active — call stop() first")
            return

        take_dir = self._sm.take_path(session, take)
        csv_path = self._sm.csv_path(take_dir)

        if not os.path.exists(csv_path):
            log.error(f"Take not found: {csv_path}")
            return

        self.session   = session
        self.take      = take
        self.speed     = speed
        self.loop      = loop
        self.index     = 0
        self.total     = 0
        self.elapsed_s = 0.0
        self.total_s   = 0.0

        self.active = True
        self._task  = asyncio.ensure_future(
            self._replay_loop(csv_path, queue, pipeline_stages, speed, loop)
        )
        log.info(
            f"Playback started — {session}/{take} (×{speed}"
            f"{', loop' if loop else ''})"
        )

    def stop(self) -> None:
        """Cancel the replay task."""
        if self._task and not self._task.done():
            self._task.cancel()
        self.active = False
        log.info("Playback stopped")

    async def _replay_loop(
        self,
        csv_path:        str,
        queue:           asyncio.Queue,
        pipeline_stages: list,
        speed:           float,
        loop:            bool,
    ) -> None:
        """Read all CSV rows and push them onto the queue with original timing."""
        try:
            with open(csv_path, newline="") as f:
                rows = list(csv.DictReader(f))

            if not rows:
                log.warning("CSV is empty")
                return

            t0_csv       = int(rows[0]["ts_esp_us"])
            self.total   = len(rows)
            self.total_s = (int(rows[-1]["ts_esp_us"]) - t0_csv) / 1e6

            while self.active:
                for stage in pipeline_stages:
                    await stage.reset()
                self.index     = 0
                self.elapsed_s = 0.0
                t0_real        = asyncio.get_event_loop().time()
                log.info(f"Replaying {len(rows)} packets…")

                for i, row in enumerate(rows, start=1):
                    if not self.active:
                        break

                    elapsed_csv_s = (int(row["ts_esp_us"]) - t0_csv) / 1e6
                    target_real   = t0_real + elapsed_csv_s / speed
                    wait          = target_real - asyncio.get_event_loop().time()
                    if wait > 0:
                        await asyncio.sleep(wait)

                    self.index     = i
                    self.elapsed_s = elapsed_csv_s

                    packet = self._row_to_packet(row)
                    if packet is not None:
                        await queue.put(packet)

                log.info("Replay finished")
                if not loop:
                    break

        except asyncio.CancelledError:
            log.info("Replay cancelled")
        except Exception as e:
            log.error(f"Replay error: {e}")
        finally:
            await queue.put(SENTINEL)
            self.active = False

    @staticmethod
    def _row_to_packet(row: dict) -> dict | None:
        """
        Reconstruct a packet dict from a CSV row.

        For super-slot rows, all non-empty named fields are loaded; any field
        absent or blank in the CSV (deps not active when the session was
        recorded) is simply omitted from the packet.
        Returns None if type_id is missing or unknown.
        """
        raw = row.get("type_id", "")
        if not raw:
            log.debug("CSV row has no type_id — skipped")
            return None

        type_id = int(raw)
        if type_id not in PACKET_TYPES:
            log.debug(f"Unknown type_id 0x{type_id:02X} — row skipped")
            return None

        packet: dict = {
            "version":   1,
            "type":      PACKET_TYPES[type_id],
            "typeId":    type_id,
            "seq":       int(row["seq"]),
            "ts_esp_us": int(row["ts_esp_us"]),
            "ts_rx_us":  int(row["ts_rx_us"]),
        }

        for field in PAYLOAD_FIELDS.get(type_id, ()):
            v = row.get(field, "")
            if v:
                packet[field] = float(v)

        return packet
