"""
storage/pose_track.py — A take's poses, computed once, stored beside it.

A take on disk is raw wire packets: `processing_loop` writes `raw.csv` *before*
the model precisely so the recording never depends on the computation model of
the day.  The position of the wheel is a model output, so it cannot live there —
it lives in `pose.bin`, next to the CSV.

What sweeping a take needs, and why only this
---------------------------------------------
Scrubbing a cursor through a take must move the wheel without anything reaching
the bus: no frame, no event, no OSC (ADR 0004).  That rules out replaying, and
means the poses have to be readable directly.

Only the pose is precomputed, and that is the whole design (ADR 0004): every
other signal is an exponential envelope that forgets its past in ~5 τ, so it can
be recovered on demand by re-feeding a few seconds of take — *and* it depends on
a tunable, so a cache of it would die on every slider move in the Signaux tab.
The pose is the exception in both directions: `pos_x`/`pos_y` are a path
integral that no amount of re-feeding recovers, and — once the wheel dimensions
left the parameter surface (model/signals/wheel.py) — nothing tunable enters it.
A pose track therefore survives a whole tuning session without ever going stale.

One record per tick, the pose *resolved*
----------------------------------------
`t`, the quaternion, `x`, `y`, `z`.  Not the raw columns: the CSV files a simple
slot under anonymous names (`qw`…`qz`) and a super slot under named ones
(`game_rv_qw`), so a reader fetching attitude itself would have to replay
`QuantityResolver`'s arbitration and know both layouts.

File layout — little-endian throughout:

    header   28 B   magic "CYRPOSE1", flags, 3 pad, wheel_R f8, wheel_r f8
    record   36 B   t f8, qw qx qy qz f4, x y z f4   ← repeated

~3.2 MB for 15 min at 100 Hz.  `t` is f8 because it is the key everything is
looked up by and must stay exact for the length of a take; the pose is f4
because 6e-8 on a unit quaternion and 1e-5 m at 100 m are far below anything a
renderer or an operator can see, and it halves the file.

The geometry stamp is not decoration.  Measured, a 5 % error on both radii moves
`pos_x/y/z`, `height_m`, `contact_offset_m` and both movement envelopes by 5 %:
it is the *absolute scale* that matters, not the R/r ratio (only `heading_deg`
is sensitive to the ratio).  Sixteen bytes make detectable the one case that
survives geometry leaving the tunable surface — someone edits config.py, and
older tracks are at the old scale.

Streamed as it is computed
--------------------------
The model runs at 50–77× real time, so a second of computing buys a minute of
take: the computation outruns the cursor before you have finished grabbing it.
Records are fixed-size and appended, and the writer flushes on a cadence, so a
reader takes however many *whole* records are on disk right now and ignores a
partial tail.  `flags` carries "complete", set by seeking back to the header at
the end — a crashed run therefore leaves a file that reads fine and is known to
be unfinished, which is exactly what makes recomputing it safe.

One producer
------------
`PoseTrackService` keeps one task per take.  Capturing poses live during the
recording instead looked free — the model computes them anyway — but nothing
resets the model at the start of a take, so the integrator enters it carrying an
accumulated offset that a run from row 0 does not have.  Two producers of the
same file, differing subtly.
"""

import asyncio
import csv
import logging
import os
import struct

import numpy as np

import config
from model.detectors import DetectorRegistry
from model.engine import Model
from model.registry import SIGNALS
from storage.playback_engine import row_to_packet
from storage.session_manager import SessionManager

log = logging.getLogger("pose_track")

MAGIC = b"CYRPOSE1"

# magic, flags, 3 bytes of padding so the two radii land on 8-byte offsets, then
# the geometry stamp itself.
HEADER_STRUCT = struct.Struct("<8sB3xdd")
RECORD_STRUCT = struct.Struct("<d7f")

FLAG_COMPLETE = 0x01

# The record layout again, as numpy sees it — reading is a bulk operation and a
# per-record struct.unpack loop over 90 000 records is not. Kept beside RECORD
# so the two cannot drift; asserted below.
DTYPE = np.dtype([
    ("t", "<f8"),
    ("qw", "<f4"), ("qx", "<f4"), ("qy", "<f4"), ("qz", "<f4"),
    ("x", "<f4"), ("y", "<f4"), ("z", "<f4"),
])
assert DTYPE.itemsize == RECORD_STRUCT.size

COLUMNS = ("t", "qw", "qx", "qy", "qz", "x", "y", "z")

# How often the writer flushes, in records. At 100 Hz this is a second of take
# per flush, which is far finer than a cursor can be dragged and coarse enough
# that the syscall is noise next to the model tick that produced the records.
FLUSH_EVERY = 100


class PoseTrackWriter:
    """Appends poses to a track file, flushing so a reader can follow along."""

    def __init__(self, path: str, wheel_R: float, wheel_r: float,
                 flush_every: int = FLUSH_EVERY):
        self.path        = path
        self.wheel_R     = wheel_R
        self.wheel_r     = wheel_r
        self.records     = 0
        self._flush_every = flush_every
        self._file = open(path, "wb")
        self._file.write(HEADER_STRUCT.pack(MAGIC, 0, wheel_R, wheel_r))
        self._file.flush()

    def append(self, t_s: float, qw, qx, qy, qz, x, y, z) -> None:
        """
        Write one tick's pose. A missing component is stored as NaN.

        NaN rather than 0: a wheel with no gyro configured has no horizontal
        position at all, and a nought there would read downstream as a wheel
        sitting at the origin — a plausible, wrong fact. `read_poses` turns it
        back into None, the same hole `ScopeRing` keeps for the same reason.
        """
        self._file.write(RECORD_STRUCT.pack(
            t_s,
            _f(qw), _f(qx), _f(qy), _f(qz),
            _f(x), _f(y), _f(z),
        ))
        self.records += 1
        if self.records % self._flush_every == 0:
            self._file.flush()

    def close(self) -> None:
        """Flush, stamp the track complete, and close."""
        if self._file is None:
            return
        self._file.flush()
        self._file.seek(0)
        self._file.write(HEADER_STRUCT.pack(MAGIC, FLAG_COMPLETE, self.wheel_R, self.wheel_r))
        self._file.flush()
        self._file.close()
        self._file = None

    def __enter__(self) -> "PoseTrackWriter":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self.close()
        elif self._file is not None:
            # A failed run must not leave a file claiming to be complete: the
            # flag is what tells the next open whether to recompute.
            self._file.close()
            self._file = None


def _f(v) -> float:
    """A pose component as a float, with a missing one becoming NaN."""
    return float("nan") if v is None else float(v)


class TrackHeader:
    """What a track says about itself before any pose is read."""

    __slots__ = ("wheel_R", "wheel_r", "complete", "records")

    def __init__(self, wheel_R: float, wheel_r: float, complete: bool,
                 records: int):
        self.wheel_R  = wheel_R
        self.wheel_r  = wheel_r
        self.complete = complete
        self.records  = records   # whole records on disk *right now*

    def geometry_matches(self, wheel_R: float | None = None,
                         wheel_r: float | None = None) -> bool:
        """
        Was this track computed at the geometry in force now?

        Exact comparison on purpose. These are two numbers typed into config.py,
        not measurements to be compared with a tolerance, and "someone edited
        config.py" is precisely the event this is here to notice.
        """
        R = config.R_TORE if wheel_R is None else wheel_R
        r = config.r_TORE if wheel_r is None else wheel_r
        return self.wheel_R == R and self.wheel_r == r


def read_header(path: str) -> TrackHeader | None:
    """
    Read a track's header and count the whole records behind it.

    Returns None when the file is absent or too short to hold a header — which
    is a normal state, not an error: the writer has just truncated it and is
    about to write. A file that exists with a wrong magic *is* an error and
    raises, since silently treating it as absent would recompute over whatever
    it actually was.
    """
    try:
        with open(path, "rb") as f:
            raw = f.read(HEADER_STRUCT.size)
            size = os.fstat(f.fileno()).st_size
    except FileNotFoundError:
        return None

    if len(raw) < HEADER_STRUCT.size:
        return None

    magic, flags, wheel_R, wheel_r = HEADER_STRUCT.unpack(raw)
    if magic != MAGIC:
        raise ValueError(f"{path} is not a pose track (magic {magic!r})")

    # Integer division is the guard against a torn tail: the writer appends
    # whole records, so anything past the last complete one is half-written and
    # not ours to read yet.
    records = max(0, (size - HEADER_STRUCT.size) // RECORD_STRUCT.size)
    return TrackHeader(wheel_R, wheel_r, bool(flags & FLAG_COMPLETE), records)


def read_array(path: str, header: TrackHeader | None = None) -> np.ndarray:
    """Every whole record on disk, as a structured array (see DTYPE)."""
    if header is None:
        header = read_header(path)
    if header is None or header.records == 0:
        return np.empty(0, dtype=DTYPE)
    return np.fromfile(path, dtype=DTYPE, count=header.records, offset=HEADER_STRUCT.size)


def read_poses(path: str, *, start: float | None = None, end: float | None = None,
               points: int = 0, header: TrackHeader | None = None) -> dict:
    """
    The track's poses as columns, optionally windowed and thinned.

    Thinning is a *stride*, not the min/max envelope `ScopeRing` uses. The two
    answer different questions: an envelope exists so a one-sample spike still
    reads as a spike, which is what a detector triggers on; a pose is a point on
    a trajectory, and the min and max of a quaternion component over forty ticks
    is not a rotation anything could render.

    NaN comes back as None — "no position here", distinct from a position of
    zero (see PoseTrackWriter.append).
    """
    arr = read_array(path, header)

    if arr.size and (start is not None or end is not None):
        t = arr["t"]
        lo = 0 if start is None else int(np.searchsorted(t, start, side="left"))
        hi = len(t) if end is None else int(np.searchsorted(t, end, side="right"))
        arr = arr[lo:hi]

    if points > 0 and len(arr) > points:
        # Ceil division, so the stride always lands on or below the budget, and
        # the last record is appended separately — the end of the window is the
        # cursor's destination and dropping it would stop the sweep short.
        stride = -(-len(arr) // points)
        thinned = arr[::stride]
        if thinned[-1]["t"] != arr[-1]["t"]:
            thinned = np.concatenate([thinned, arr[-1:]])
        arr = thinned

    return {name: [None if v != v else float(v) for v in arr[name]]
            for name in COLUMNS}


# ── Computing one ────────────────────────────────────────────────────────────

def compute_pose_track(csv_path: str, out_path: str, *,
                       wheel_R: float | None = None, wheel_r: float | None = None,
                       flush_every: int = FLUSH_EVERY) -> int:
    """
    Feed a take's CSV through the model from row 0 and write its poses.

    Blocking and CPU-bound — call it through `asyncio.to_thread`. Measured, a
    100 Hz ticker goes from a 16 ms p95 to 24 ms during one of these, without
    ever dropping out: CPython yields the GIL every 5 ms, so a subprocess buys
    nothing (ADR 0004).

    The model is a private instance with its own registry and *no* detectors:

      * no bus, so nothing it computes reaches a subscriber, and warming it up
        cannot fire a handful of impacts into Live;
      * an isolated registry, so the file does not depend on which signals
        happen to be switched off in the Signaux tab, and the panel's error
        counters do not move because a take was opened;
      * no detectors at all — this wants poses, and running the detector graph
        would be work spent on events nobody will ever see.

    Nothing about it is a second computation path: it is `model.feed()` going
    forward, the same one live uses, which is the whole condition ADR 0003 puts
    on a precomputed result.
    """
    R = config.R_TORE if wheel_R is None else wheel_R
    r = config.r_TORE if wheel_r is None else wheel_r

    model = Model(
        bus        = None,
        max_gap_us = int(config.MAX_DT_S * 1e6),
        registry   = SIGNALS.isolated(),
        detectors  = DetectorRegistry(),
    )

    with open(csv_path, newline="") as f, \
         PoseTrackWriter(out_path, R, r, flush_every) as writer:
        for row in csv.DictReader(f):
            packet = row_to_packet(row)
            if packet is None:
                continue
            frame = model.feed(packet)
            if frame is None:
                continue
            p = frame.pose
            writer.append(
                frame.t_us / 1e6,
                p.get("qw"), p.get("qx"), p.get("qy"), p.get("qz"),
                p.get("x"), p.get("y"), p.get("z"),
            )
        records = writer.records

    log.info(f"Pose track computed — {records} poses → {out_path}")
    return records


# ── The single producer ──────────────────────────────────────────────────────

class PoseTrackService:
    """
    Computes pose tracks on demand, one task per take, never two.

    Triggered from two places, both of which mean "this take is now worth
    having a track for": the end of a recording, and the first read of a track
    that does not exist yet.
    """

    def __init__(self, session_manager: SessionManager):
        self._sm    = session_manager
        self._tasks: dict[tuple[str, str], asyncio.Task] = {}

        # The two ways a track can be unusable, remembered apart because they
        # stop being true at different moments. A file that is not a pose track
        # ceases to be a problem the instant it is deleted, so that memory is
        # dropped as soon as the file is gone — which is what makes "delete
        # pose.bin and reopen the take" a recovery that actually works. A
        # computation that raised, on the other hand, will raise again on the
        # same unchanged CSV, so it sticks for the process: a panel polling at
        # 4 Hz must not spawn a doomed thread four times a second.
        self._unreadable: dict[tuple[str, str], str] = {}
        self._failed:     dict[tuple[str, str], str] = {}

    # ── Paths ────────────────────────────────────────────────────────────────

    def path(self, session: str, take: str) -> str:
        return self._sm.pose_path(self._sm.take_path(session, take))

    # ── Observation ──────────────────────────────────────────────────────────

    def status(self, session: str, take: str) -> dict:
        """
        Where this take's track stands: absent, computing, ready, partial or
        failed.

        `records` and `duration_s` are read from the file every time rather than
        tracked in memory, so progress is reported the same whether this process
        computed the track or found it already there.
        """
        key  = (session, take)
        path = self.path(session, take)
        return self._describe(key, path, self._header(key, path))

    async def read(self, session: str, take: str, *, start: float | None = None,
                   end: float | None = None, points: int = 0) -> dict:
        """
        The poses *and* where the track stands, from one look at the file.

        One header read feeds both halves on purpose: the writer may flush again
        between two reads, and a reply whose `records` disagreed with the number
        of poses beside it would leave a caller unable to tell "still filling"
        from "something is wrong".

        The bulk of it goes through `asyncio.to_thread` — turning 90 000 records
        into Python floats is hundreds of milliseconds, and the loop this would
        run on is the one owning `processing_loop`, which can never drop a
        packet.  Only the header read stays inline: it is 28 bytes and a stat,
        and it touches this object's own bookkeeping, which belongs to the loop.
        """
        key    = (session, take)
        path   = self.path(session, take)
        header = self._header(key, path)

        out = self._describe(key, path, header)
        # A header of None also covers "unreadable, and _header swallowed why",
        # so the file is not touched again — read_poses would re-raise it.
        if header is None:
            out["poses"] = {c: [] for c in COLUMNS}
        else:
            out["poses"] = await asyncio.to_thread(
                read_poses, path, start=start, end=end,
                points=points, header=header,
            )
        out["count"] = len(out["poses"]["t"])
        return out

    def _describe(self, key: tuple[str, str], path: str,
                  header: TrackHeader | None) -> dict:
        error = self._failed.get(key) or self._unreadable.get(key)

        if key in self._tasks:
            state = "computing"
        elif error is not None:
            state = "failed"
        elif header is None:
            state = "absent"
        elif header.complete:
            state = "ready"
        else:
            # On disk, not complete, nobody computing it: a run that died.
            # `ensure` restarts it; on its own this is just a fact to report.
            state = "partial"

        out = {
            "status":     state,
            "records":    header.records if header else 0,
            "complete":   bool(header and header.complete),
            "duration_s": self._duration(path, header),
            "error":      error,
            "geometry":   None,
        }
        if header is not None:
            out["geometry"] = {
                "R_TORE":  header.wheel_R,
                "r_TORE":  header.wheel_r,
                "current": {"R_TORE": config.R_TORE, "r_TORE": config.r_TORE},
                # False means the track was computed at another scale and its
                # positions are wrong by that ratio. Reported, never repaired
                # silently — recomputing behind the user's back would throw away
                # the one clue that config.py changed.
                "matches": header.geometry_matches(),
            }
        return out

    # ── Producing ────────────────────────────────────────────────────────────

    async def ensure(self, session: str, take: str) -> dict:
        """
        Make sure a track exists for this take, and say where it stands.

        Returns immediately: the computation runs in a worker thread and the
        track is readable as it fills.  It is a coroutine because it must run on
        the event loop thread — `ensure_future` needs one — not because it
        waits: there is no `await` before `_tasks[key] = …`, and that is exactly
        what makes "one producer" true, since two concurrent callers cannot both
        find the slot empty.
        """
        key    = (session, take)
        path   = self.path(session, take)
        header = self._header(key, path)

        started = (
            key not in self._tasks
            and not (header is not None and header.complete)
            # An unreadable file, or a run that raised. Recomputing over it
            # would destroy the one clue about what went wrong; the caller is
            # told instead, and the recovery is to remove the file.
            and key not in self._failed
            and key not in self._unreadable
        )
        if started:
            csv_path = self._sm.csv_path(self._sm.take_path(session, take))
            if not os.path.exists(csv_path):
                raise FileNotFoundError(csv_path)
            self._tasks[key] = asyncio.ensure_future(
                self._compute(key, csv_path, path)
            )

        return self._describe(key, path, header)

    async def _compute(self, key: tuple[str, str], csv_path: str, out_path: str) -> None:
        try:
            await asyncio.to_thread(compute_pose_track, csv_path, out_path)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # Contained like a signal's failure: the take stays usable, the
            # panel gets told why, and nothing else in the orchestrator notices.
            self._failed[key] = str(e)
            log.exception(f"Pose track computation failed for {key[0]}/{key[1]}")
        finally:
            self._tasks.pop(key, None)

    def cancel_all(self) -> None:
        """
        Stop waiting on every running computation — used on shutdown.

        Cancelling the coroutine does not interrupt the worker thread it is
        awaiting: `asyncio.to_thread` has no way to, so a computation already
        under way runs to its end and stamps its track complete, which is fine.
        What this buys is that shutdown does not block on it.  A track caught
        genuinely mid-write — the process killed outright — is the case the
        completion flag exists for.
        """
        for task in self._tasks.values():
            task.cancel()
        self._tasks.clear()

    # ── Internals ────────────────────────────────────────────────────────────

    def _header(self, key: tuple[str, str], path: str) -> TrackHeader | None:
        """`read_header`, deciding what to remember about a file that will not."""
        try:
            header = read_header(path)
        except ValueError as e:
            # Not a pose track at all. A fact about the file, so remembered
            # until the file changes.
            self._unreadable[key] = str(e)
            return None
        except OSError as e:
            # A busy disk, a permission being repaired: transient by nature.
            # Reported for this call and deliberately not remembered, so the
            # next open simply tries again.
            log.warning(f"Pose track unreadable at {path}: {e}")
            return None

        if header is None:
            # Nothing on disk. Whatever was wrong with the file is gone with it,
            # which is what makes deleting it the documented recovery. A failed
            # *computation* is not cleared here — it would raise again on the
            # same CSV, and clearing it would restart the doomed run each poll.
            self._unreadable.pop(key, None)
        return header

    @staticmethod
    def _duration(path: str, header: TrackHeader | None) -> float:
        """The last pose's timestamp — the take time the track reaches so far."""
        if header is None or header.records == 0:
            return 0.0
        offset = HEADER_STRUCT.size + (header.records - 1) * RECORD_STRUCT.size
        with open(path, "rb") as f:
            f.seek(offset)
            raw = f.read(RECORD_STRUCT.size)
        if len(raw) < RECORD_STRUCT.size:
            return 0.0
        return round(RECORD_STRUCT.unpack(raw)[0], 6)
