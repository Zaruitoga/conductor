"""
storage/seek.py — Resuming a take at a chosen instant: the warm-up and the seed.

The replay itself does not change: it stays on the queue, `processing_loop` and
`model.feed()`.  What a jump adds is a **warm-up** in front of it — the model is
re-fed a few seconds of take at full CPU, into a private instance, and that
instance is then put in place of the live one.  A fast-forward from row 0 would
cost ~25 s for 15 min at 100 Hz; this costs the same tenth of a second wherever
in the take one lands, because the window is bounded by τ and by nothing else.

Why so little is enough
-----------------------
Because almost nothing in the model has a long memory.  Attitude is *stateless*
— it comes out of the packet.  Every envelope is written `ctx.alpha(tau)` and so
forgets exponentially, in about 5 τ.  Measured on a real take: after 0,5 s of
re-feeding, 14 signals out of 18 are within 0,1 % of a run from row 0 (the whole
quaternion exactly), 16 after 1 s, 17 after 8 s (ADR 0004).

The eighteenth is the horizontal position, and it never converges: it is a path
integral, irrecoverable by construction.  That one is not computed — it is
*read*, from the pose track beside the take, and planted in the integrator once
the warm-up is over (`seed_position`).  The track **is** the state exponential
forgetting does not give back.

Two things that cost dearly if missed
-------------------------------------
  * **The warm-up runs bus-less.**  Re-feeding makes the detectors fire, and
    those events must not reach the bus — a jump would otherwise send a handful
    of impacts into Live at every drag of the cursor.  `Model.twin()` is the
    bus-less instance; nothing here filters anything, there is simply nowhere
    for an event to go.
  * **The seed comes after the warm-up, never before.**  Seeding first would
    have the run integrate *away* from the planted position over the window.

Nothing here is a second computation path: it is `model.feed()` going forward,
the same one live uses, which is the whole condition ADR 0003 puts on a
precomputed result.  The rule is not "no cache", it is "no second model".
"""

import logging
import time
from dataclasses import asdict, dataclass

import config
from model.params import PARAMS
from model.signals.dynamics import SHOCK_BASELINE_TAU_S, seed_position
from storage.playback_engine import row_to_packet
from storage.pose_track import read_header, read_pose_at

log = logging.getLogger("seek")

# An exponential envelope is within ~0.7 % of its target after 5 τ, which is
# below what any of this is read at. The number is a property of `ctx.alpha`,
# not a taste — see model/registry.py.
WARMUP_TAUS = 5.0

# `accel_shock_ms2` smooths its baseline with a time constant that is not a
# parameter, so `max_tau_s()` cannot see it. This floor is the only reason the
# window is not purely derived: it keeps a profile whose every declared τ has
# been turned right down from leaving that baseline short. Imported rather than
# repeated — the same number in two files is one edit away from a warm-up that
# silently stops converging.
MIN_WINDOW_S = WARMUP_TAUS * SHOCK_BASELINE_TAU_S


def warmup_window_s(params=PARAMS) -> float:
    """
    How far back a warm-up has to start to land in the same state as a full run.

    Derived from the τ actually declared (`PARAMS.declare(..., tau=True)`) and
    from their *current* values, so raising a time constant widens the window by
    itself.  Guessing it instead — a constant here, a name convention there —
    would leave whichever signal outgrew it converging silently short.
    """
    return max(MIN_WINDOW_S, WARMUP_TAUS * params.max_tau_s())


@dataclass(frozen=True, slots=True)
class SeekReport:
    """
    What one jump did, for the panel and for a log line.

    One shape whatever happened, including a jump that failed outright: a
    consumer reading `playback.seek.rows` should not have to find out that the
    key is missing today because something else went wrong.
    """
    target_s: float
    window_s: float          # what chose the rows below
    rows:     int            # rows re-fed
    ticks:    int            # ticks the warm-up produced
    seeded:   bool           # was the position planted from the pose track
    reason:   str | None     # why it was not, when it was not
    geometry_matches: bool | None   # None when there is no track to compare
    ms:       float          # what the whole warm-up cost
    error:    str | None = None     # the jump did not happen at all

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def failed(cls, target_s: float, error: str) -> "SeekReport":
        """A jump that raised. Reported, never silent — the replay carries on."""
        return cls(target_s=round(target_s, 6), window_s=0.0, rows=0, ticks=0,
                   seeded=False, reason=None, geometry_matches=None, ms=0.0,
                   error=error)


def warm_to(live, rows: list[dict], target_s: float, *, origin_s: float = 0.0,
            track_path: str | None = None, window_s: float = 0.0):
    """
    Bring a private copy of `live` to `target_s`, and hand it back with a report.

    Blocking and CPU-bound — call it through `asyncio.to_thread`, like the pose
    track computation it borrows its measurements from (ADR 0004: a 100 Hz
    ticker goes from a 16 ms p95 to 24 ms during one, without dropping out).

    `origin_s` is the take time the first of `rows` sits at, and anchors the
    twin's timeline there: the model's clock counts from its first sample, so
    without it the replay would resume announcing a `frame.t` of a few seconds
    while the cursor sat at thirty.

    `window_s` is *recorded*, not applied — `rows` already is the window, and
    the caller is who chose it (`warmup_window_s()`, then
    `PlaybackEngine.warmup_rows`).  Selecting rows is the engine's job: it owns
    the take's timeline, and re-deriving the slice here would be a second answer
    to the same question.

    The returned model is *not* live yet: it has no bus and has not taken over
    the event numbering.  Both belong to the substitution, which happens back on
    the event loop in one uninterrupted step (see core.seek_model) — the twin is
    only warm here.
    """
    t0 = time.perf_counter()

    warm = live.twin()
    warm.start_at(origin_s)
    ticks = 0
    for row in rows:
        packet = row_to_packet(row)
        if packet is None:
            continue
        if warm.feed(packet) is not None:
            ticks += 1

    seeded, reason, matches = _seed(warm, track_path)

    report = SeekReport(
        target_s = round(target_s, 6),
        window_s = round(window_s, 3),
        rows     = len(rows),
        ticks    = ticks,
        seeded   = seeded,
        reason   = reason,
        geometry_matches = matches,
        ms       = round((time.perf_counter() - t0) * 1000.0, 1),
    )
    log.info(
        f"Warm-up to {target_s:.3f}s — {report.rows} rows, {ticks} ticks, "
        f"{report.ms} ms, seeded={seeded}"
        + (f" ({reason})" if reason else "")
    )
    return warm, report


def _seed(warm, track_path: str | None):
    """
    Plant the position from the take's pose track. Returns (seeded, why, geom).

    The instant used is the one the model has actually reached
    (`Model.last_tick_s`), never the instant asked for: the warm-up stops on the
    row *before* the one the replay resumes on, and that row then integrates one
    step of its own.  Planting the destination instead would overshoot by a
    whole tick — nine centimetres at 2 m/s, which is precisely the teleport the
    seed exists to remove.

    Every absence is a *reason*, never an exception: a take whose track has not
    been computed yet, or is still filling short of the instant reached, must
    still be seekable — the wheel simply resumes from wherever the warm-up's own
    integration landed, which is the pre-existing behaviour and not a fault.

    A track computed at another wheel geometry is used all the same, and said
    so.  Its positions are wrong by that ratio, but they are the very positions
    the sweep is drawing from the same file: refusing to seed would trade a
    known scale error for a visible teleport, and ADR 0004 asks for a mismatch
    to be *reported*, not repaired behind the operator's back.
    """
    if not track_path:
        return False, "aucune piste de pose", None

    # Before the first tick there is no position state to correct, and the tick
    # that comes carries no elapsed time to integrate — nothing to plant.
    at_s = warm.last_tick_s
    if at_s is None:
        return False, "aucun tick pendant la mise en régime", None

    matches = None
    try:
        header = read_header(track_path)
        if header is None or header.records == 0:
            return False, "piste absente ou vide", None
        matches = header.geometry_matches()
        pose = read_pose_at(track_path, at_s, header=header)
    except (ValueError, OSError) as e:
        # Not a pose track, or unreadable right now — a file being written over,
        # a disk hiccup. Both reads are inside this because the jump itself has
        # already succeeded by the time they run: failing it over a position
        # that could not be read would throw away a warm-up for nothing.
        return False, f"piste illisible ({e})", matches

    if pose is None:
        return False, "piste vide avant cet instant", matches
    if pose["x"] is None or pose["y"] is None:
        # A take recorded without a gyro has no horizontal position at all —
        # NaN in the track, None here. Nothing to plant, and a nought would
        # read downstream as a wheel sitting at the origin.
        return False, "pas de position horizontale dans la piste", matches
    if not header.complete and pose["t"] < at_s:
        # Still filling, and it has not got this far: the last pose it holds is
        # from somewhere else in the take, so planting it would be worse than
        # not seeding at all.
        return False, "piste incomplète avant cet instant", matches
    if abs(pose["t"] - at_s) > config.MAX_DT_S:
        # The track's `t` is the *model's* timeline, which holds still across a
        # dropout it refuses to integrate; the instant reached here descends
        # from the replay's pacing clock, which waits that same dropout out
        # (deliberately — storage/playback_engine.py). A take with a long hole
        # in it therefore has the two drifting apart, and a pose fetched at the
        # wrong instant plants a position from somewhere else along the take.
        # Bounded by the gap the model itself will not integrate: past that,
        # say so instead of planting.
        return False, f"piste décalée de {pose['t'] - at_s:+.2f}s ici", matches

    seed_position(warm, pose["x"], pose["y"])
    return True, None, matches
