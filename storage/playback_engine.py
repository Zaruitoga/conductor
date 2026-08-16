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

Seeking
-------
Resuming at a chosen instant is a *cursor move* here and nothing more: the rows
are already in memory, so the jump costs the same whatever the take's length or
the distance travelled.  What the model needs to be in the right state at that
instant — a warm-up over the preceding seconds, and the position read off the
pose track — is not this engine's business (it does not know a model exists,
exactly as `on_reset` records).  It is handed in as `on_seek`, and awaited from
*inside* the replay loop: while the warm-up runs, the loop is parked in it, so
it cannot push a row of the old position into a model that is being replaced.
See storage/seek.py and ADR 0004.
"""

import asyncio
import bisect
import csv
import logging
import os

from model.clock import TimeBase
from storage.session_manager import SessionManager
from transport.protocol import ALL_SUPER_NAMED_FIELDS

log = logging.getLogger("playback_engine")

# Gap tolerance for the *pacing* clock, deliberately looser than the model's:
# a real dropout inside a take should still be waited out rather than collapsed,
# while a 32-bit rollover (which would unwrap to ~700 s from a reboot) is still
# rejected. See model/clock.py for how the two are told apart.
_PACING_MAX_GAP_US = 5_000_000

# Must stay in sync with udp_receiver.py — except type 0x20 (heartbeat), which
# is telemetry never written to the CSV, so there is nothing to replay for it.
PACKET_TYPES: dict[int, str] = {
    0x01: "gyro",         0x02: "accel",    0x03: "mag",
    0x04: "linear_accel", 0x05: "rv",       0x06: "geo_rv",
    0x07: "game_rv",      0x08: "arvr_rv",
}
for _i in range(8):
    PACKET_TYPES[0x10 + _i] = f"super_{_i}"

_VEC3_FIELDS = ("x", "y", "z")
_QUAT_FIELDS = ("qw", "qx", "qy", "qz")

PAYLOAD_FIELDS: dict[int, tuple[str, ...]] = {
    0x01: _VEC3_FIELDS,  0x02: _VEC3_FIELDS,
    0x03: _VEC3_FIELDS,  0x04: _VEC3_FIELDS,
    0x05: _QUAT_FIELDS,  0x06: _QUAT_FIELDS,
    0x07: _QUAT_FIELDS,  0x08: _QUAT_FIELDS,
    # Super types: scan all named fields; only populated columns are loaded
    **{0x10 + i: ALL_SUPER_NAMED_FIELDS for i in range(8)},
}

SENTINEL = {"typeId": "playback_end"}


def row_to_packet(row: dict) -> dict | None:
    """
    Reconstruct a packet dict from a CSV row.

    For super-slot rows, all non-empty named fields are loaded; any field
    absent or blank in the CSV (deps not active when the session was
    recorded) is simply omitted from the packet.
    Returns None if type_id is missing or unknown.

    Module-level rather than a method because it is the *only* decoder of the
    CSV's layout, and the pose-track computation (storage/pose_track.py) reads
    the same files.  Two decoders would be two chances to disagree about which
    column a super slot's gyro landed in.
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


def row_index_at(offsets_us: list[int], t_s: float) -> int:
    """
    The row a replay should resume on for a take time, bounded to the take.

    The first row at or after `t_s`, so that every packet of that instant is
    replayed rather than half of them: a simple-slot recording files attitude
    and gyro under the same timestamp in consecutive rows.  Both ends are
    bounded rather than refused — a jump past the end lands on the last row and
    the take finishes at once, a negative one lands on row 0.
    """
    if not offsets_us:
        return 0
    i = bisect.bisect_left(offsets_us, int(t_s * 1e6))
    return min(max(i, 0), len(offsets_us) - 1)


class PlaybackEngine:
    """Replays a recorded CSV session as a stream of packets."""

    def __init__(self, session_manager: SessionManager):
        self._sm    = session_manager
        self._task: asyncio.Task | None = None
        self.active = False

        # Set to wake the replay loop out of a pause — by a resume, by a seek,
        # or by stop(). One event for the three because a seek has to be
        # honoured *while* paused: the operator drags the cursor, then presses
        # play, and the warm-up must already have happened by then rather than
        # delaying the first packet.
        self._wake = asyncio.Event()

        # The take, in memory. A seek is a cursor move over these two, which is
        # what makes its cost independent of the take's length.
        self._rows:    list[dict] = []
        self._offsets: list[int]  = []      # take-relative µs, one per row

        # Pending seek target in take seconds, or None. Deliberately one slot
        # rather than a queue: dragging a cursor produces a stream of requests
        # and only the last one is worth a warm-up.
        self._seek_to: float | None = None

        # Progress, exposed via GET /api/playback/status.
        self.paused:    bool       = False
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
        session:  str,
        take:     str,
        queue:    asyncio.Queue,
        on_reset: callable,
        speed:    float = 1.0,
        loop:     bool  = False,
        on_seek:  callable = None,
        start_s:  float | None = None,
    ) -> None:
        """
        Start replaying a take in the background.

        `on_reset` is called before the first packet of every pass so that
        integrators and envelopes start clean — a replay that inherited the
        previous run's state would not be reproducible, which is the whole point
        of replaying.  A callable rather than a list of stages: what needs
        resetting is the model's business, not this engine's.

        `on_seek(t_s)` is awaited when a seek is honoured, before any packet of
        the new position is queued, and is handed the exact take time the replay
        will resume on — the row's own offset, not the requested instant.  Same
        reasoning as `on_reset`: bringing the model to that instant is the
        model's business.

        `start_s` is where the *first* pass begins — a cursor placed by sweeping
        an idle take, which `seek` cannot serve because there is no replay to
        talk to yet.  It is a pending seek and nothing else, so the loop applies
        it through the same `_settle` before queueing a single row: starting at
        zero and jumping straight after would play the take's opening for the
        length of a round trip.  It belongs to that first pass alone — a looping
        replay is replaying the take, not that ten seconds of it.
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
        self.paused    = False
        self._rows     = []
        self._offsets  = []
        # `clamp_time` can only judge the negative side here — the take is not
        # loaded yet, so `total_s` is 0 — and `row_index_at` bounds the other.
        self._seek_to  = None if start_s is None else self.clamp_time(start_s)
        self._wake.set()

        self.active = True
        self._task  = asyncio.ensure_future(
            self._replay_loop(csv_path, queue, on_reset, speed, loop, on_seek)
        )
        log.info(
            f"Playback started — {session}/{take} (×{speed}"
            f"{', loop' if loop else ''}"
            f"{f', from {self._seek_to:.3f}s' if start_s is not None else ''})"
        )

    def stop(self) -> None:
        """Cancel the replay task."""
        if self._task and not self._task.done():
            self._task.cancel()
        self.active   = False
        self.paused   = False
        self._seek_to = None
        self._wake.set()   # a paused loop must not stay blocked
        log.info("Playback stopped")

    def pause(self) -> None:
        """Freeze the replay where it is; timing is realigned on resume()."""
        self.paused = True
        log.info(f"Playback paused at {self.elapsed_s:.1f}s")

    def resume(self) -> None:
        """Resume a paused replay."""
        self.paused = False
        self._wake.set()
        log.info("Playback resumed")

    # ── Seeking ──────────────────────────────────────────────────────────────

    def clamp_time(self, t_s: float) -> float:
        """
        A take time bounded to the take's own length.

        Bounded rather than refused at both ends: past the end is "play the end
        of it", which is what dragging a cursor off the right of a bar means.
        Before the rows are loaded `total_s` is still 0, and only the negative
        side can be judged — `row_index_at` bounds the other.
        """
        t = max(0.0, float(t_s))
        return t if self.total_s <= 0 else min(t, self.total_s)

    def seek(self, t_s: float) -> float:
        """
        Ask the replay to resume at a take time. Returns the accepted target.

        Returns immediately: the request is a note the replay loop reads before
        it emits its next packet, so a drag that produces thirty requests a
        second costs one warm-up, not thirty.  `seek_target_s` reports one still
        outstanding — including while paused, where it stays pending until the
        loop next runs.
        """
        target = self.clamp_time(t_s)
        self._seek_to = target
        self._wake.set()
        log.info(f"Seek requested → {target:.3f}s")
        return target

    @property
    def seek_target_s(self) -> float | None:
        """The take time a requested seek is heading for, until it is applied."""
        return self._seek_to

    def warmup_rows(self, t_s: float, window_s: float) -> tuple[list[dict], float]:
        """
        The rows covering `window_s` of take before `t_s`, and where they start.

        Half-open at the top — the row the replay resumes on is excluded — so
        every row is fed exactly once across the jump, rather than the warm-up
        and the replay both handing the model the same instant.

        The window is counted from the row actually resumed on, so the answer
        matches what `row_index_at` picks rather than the raw request.

        The start time comes back with the rows because a model's timeline is
        the take's: a warm-up anchored at zero would have the replay resume
        reporting a `frame.t` of a few seconds while the cursor sat at thirty
        (see `Model.start_at`).
        """
        if not self._offsets:
            return [], 0.0
        end   = row_index_at(self._offsets, t_s)
        start = row_index_at(self._offsets, self._offsets[end] / 1e6 - window_s)
        return self._rows[start:end], self._offsets[start] / 1e6

    def _load(self, csv_path: str) -> bool:
        """
        Read the take into memory and build its take-relative timeline.

        The timeline is unwrapped once for the whole file. The raw `ts_esp_us`
        is a uint32 that rolls over every 71 min 35 s: subtracting it directly
        made every row after a rollover come out negative, so a long take
        replayed its entire tail in one burst and reported a negative duration.
        See model/clock.py.

        Returns False on an empty CSV, which is the one case there is nothing to
        replay from.
        """
        with open(csv_path, newline="") as f:
            rows = list(csv.DictReader(f))

        if not rows:
            log.warning("CSV is empty")
            return False

        clock   = TimeBase(_PACING_MAX_GAP_US)
        offsets = [clock.update(int(r["ts_esp_us"])).t_us for r in rows]
        if clock.wraps:
            log.info(f"Take crosses {clock.wraps} counter rollover(s) — unwrapped")

        self._rows, self._offsets = rows, offsets
        self.total   = len(rows)
        self.total_s = offsets[-1] / 1e6
        return True

    async def _replay_loop(
        self,
        csv_path: str,
        queue:    asyncio.Queue,
        on_reset: callable,
        speed:    float,
        loop:     bool,
        on_seek:  callable = None,
    ) -> None:
        """Read all CSV rows and push them onto the queue with original timing."""
        try:
            if not self._load(csv_path):
                return

            rows, offsets = self._rows, self._offsets
            now = asyncio.get_event_loop().time

            while self.active:
                on_reset()
                i              = 0
                self.index     = 0
                self.elapsed_s = 0.0
                t0_real        = now()
                log.info(f"Replaying {len(rows)} packets…")

                while self.active and i < len(rows):
                    # Settled before anything is emitted, so the warm-up a seek
                    # runs happens with the loop parked inside it — it cannot
                    # push a row of the old position at a model being replaced.
                    if self._seek_to is not None or self.paused:
                        i, t0_real = await self._settle(i, on_seek, speed, t0_real)
                        continue

                    elapsed_csv_s = offsets[i] / 1e6
                    target_real   = t0_real + elapsed_csv_s / speed
                    wait          = target_real - now()
                    if wait > 0:
                        await asyncio.sleep(wait)
                        # That sleep is where a seek request lands, and this row
                        # belongs to where we no longer are: go settle instead
                        # of emitting it.
                        if self._seek_to is not None or self.paused:
                            continue

                    self.index     = i + 1
                    self.elapsed_s = elapsed_csv_s

                    packet = row_to_packet(rows[i])
                    if packet is not None:
                        await queue.put(packet)
                    i += 1

                log.info("Replay finished")
                if not loop:
                    break

        except asyncio.CancelledError:
            log.info("Replay cancelled")
        except Exception as e:
            log.error(f"Replay error: {e}")
        finally:
            await queue.put(SENTINEL)
            self.active   = False
            self.paused   = False
            self._seek_to = None
            self._wake.set()

    async def _settle(self, i: int, on_seek: callable, speed: float,
                      t0_real: float) -> tuple[int, float]:
        """
        Apply any pending seek and hold while paused. Returns (row, time base).

        Both in one wait, because a seek requested while paused must be honoured
        then and there rather than at the next play — otherwise the warm-up's
        cost would land on the play button instead of on the drag that asked
        for it.

        Deadlines in the loop are absolute (`t0_real + elapsed/speed`), so both
        cases have to move the time base: a pause pushes it forward by however
        long it lasted, or the backlog would replay in one burst; a seek rebases
        it outright so the row landed on is due now.
        """
        now = asyncio.get_event_loop().time

        while self.active:
            target = self._seek_to
            if target is not None:
                self._seek_to = None
                i        = row_index_at(self._offsets, target)
                resume_s = self._offsets[i] / 1e6 if self._offsets else 0.0
                if on_seek is not None:
                    await on_seek(resume_s)
                self.index     = i
                self.elapsed_s = resume_s
                t0_real        = now() - resume_s / speed
                log.info(f"Seek applied — row {i}, {resume_s:.3f}s")
                continue

            if not self.paused:
                return i, t0_real

            t_pause = now()
            self._wake.clear()
            await self._wake.wait()
            t0_real += now() - t_pause

        return i, t0_real

