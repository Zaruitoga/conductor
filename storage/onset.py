"""
storage/onset.py — Where a take's movement starts, proposed from the raw gyro.

A take is aligned to its video by one event: the wheel lies still on the ground,
then it is lifted in a sharp gesture.  Found on both sides, that instant is the
pair of anchors an alignment consists of.  This module finds the inertial one.

It only ever *proposes* (ADR 0001).  What has authority is the pair a human
confirmed, and it is never recalculated behind their back — which is precisely
what lets the rule below be retuned forever without moving an alignment already
made.  Nothing here is stored: a proposition on disk would be a number nobody
can date (computed with which threshold, before or after the last change?), so
it is recomputed on demand for about a second of CSV reading.

The rule, entire
----------------
**The anchor is the first sample that ends a silence of at least 2 seconds**,
silence being the raw gyro norm under 0.5 rad/s.  Two constants, no hysteresis,
no flatness criterion.

Measured on the four reference takes, the pattern happens two to four times per
take — the wheel is set down again between attempts — and "immobile, then a
crossing" alone picked the right instant one take in three.  What saves the rule
is that the *first* rest of the recording is the one that counts, not the
longest: the gesture happens at the beginning, and a repeat later is ignored.
The others are still returned, because overruling the rule should be a choice
between candidates rather than free pointing at a curve.

No hysteresis, unlike `model/detectors.py`: hysteresis exists to re-arm a
detector that must fire again, and this fires once.  "At least 2 s of silence"
*is* the debounce, in a stronger form.

**0.5 rad/s comes from what the camera resolves, not from taste.**  A point on
the rim, one metre out, between two frames of 33 ms: 0.15 rad/s moves it 5 mm
(~1 px — invisible), 0.5 rad/s moves it 17 mm (~3 px — the edge of visibility),
1.0 rad/s moves it 33 mm.  The BNO08x latches an exact zero at rest and the
wheel runs at 5–20 rad/s, so telling still from moving has two and a half orders
of margin and is not what the threshold decides.  What it decides is the
*tremble*: take 004 stirs between 0.02 and 0.29 rad/s for 580 ms before the real
gesture, and a threshold of 0.15 would anchor there — 481 ms, 14 frames early,
with nothing to say so, aligning "the hand touches it" with "the wheel moves".

Two halves, deliberately
------------------------
`read_gyro_norm` reads the CSV, `propose` decides — so a test of the rule is a
synthetic `(t, |ω|)` array and not a fabricated file, and so the decision can be
re-run on a curve already in hand.  The curve goes out whole, unreduced
(ADR 0002): the browser reduces it to min/max envelopes at the resolution it is
looking at, which makes zooming free, and zooming is the whole activity of
checking an alignment.
"""

import csv
import math

import config
from model.clock import TimeBase
from storage.playback_engine import row_to_packet
from transport.protocol import SLOT_FIELDS, SUPER_BASE, SUPER_MAX

# The rule's two constants. Deliberately not `PARAMS.declare(...)`: a tunable
# would make a proposition depend on a setting, which is what ADR 0001 rules
# out, and neither number moved the answer over the range that was considered
# (0.15–0.5 rad/s, 2–3 s) on the takes they were measured from.
SILENCE_MAX_RAD_S = 0.5
SILENCE_MIN_S     = 2.0

# Why there is nothing to propose, in the words the alignment page shows. The
# two are not the same fact and must not collapse into an empty list: "no gyro
# stream" means the method does not apply here (take 001 of the reference
# session is exactly that — 171 rows, GAME_RV only), while the other means it
# ran and found nothing.
NO_GYRO  = "aucun flux gyro"
NO_ONSET = (f"aucun silence d'au moins {SILENCE_MIN_S:g} s "
            f"suivi d'un franchissement")

# Which columns hold the gyro, per packet type — the same shape as
# `csv_logger.PAYLOAD_FIELDS`, and for the same reason: a CSV files a *simple*
# slot's vector in the anonymous columns and a *super* slot's under its sensor's
# name (issue #12).  The reference session recorded the first — two simple slots,
# GYRO and GAME_RV at 50 Hz — so reading only the named ones would work against
# every fixture in this repo and against none of the real takes.
#
# The named triple comes from `protocol.SLOT_FIELDS`, the registry CLAUDE.md
# warns must not be retyped.  The anonymous one has no registry to come from:
# `parse_packet` spells it out for every Vec3 type, which is issue #12 itself.
_GYRO_SLOT = 0                    # SLOT_NAME[0] == "GYRO"
_GYRO_TYPE = 0x01                 # TYPE_NAME[0x01], the simple gyro slot

_GYRO_FIELDS: dict[int, tuple[str, ...]] = {
    _GYRO_TYPE: ("x", "y", "z"),
    **{t: SLOT_FIELDS[_GYRO_SLOT] for t in range(SUPER_BASE, SUPER_MAX + 1)},
}

# Resolution of the curve on the wire. Time is rounded to the microsecond, which
# loses nothing — the timeline *is* integer microseconds — and keeps the JSON
# short. The norm keeps 0.1 mrad/s, a step below the BNO08x's own quantum
# (~2 mrad/s) and four orders below the threshold. Both are rounded here rather
# than at serialisation so that the proposition and the curve an operator checks
# it against are computed from the same numbers.
_T_DIGITS = 6
_V_DIGITS = 4


# ── The rule ────────────────────────────────────────────────────────────────

def propose(samples, *, threshold: float = SILENCE_MAX_RAD_S,
            min_silence_s: float = SILENCE_MIN_S) -> dict:
    """
    Every sample that ends a long enough silence, in order, plus why there is
    none.  Pure: `samples` is any sequence of `(t_s, |ω|)` pairs.

        {"candidats": [{"t_s": 11.342, "silence_s": 11.322}, …],
         "motif":     None | NO_GYRO | NO_ONSET}

    The anchor is the first sample **after** the rest, never the last one
    before: at 50 Hz that is 20 ms, always in the same direction, and the video
    side is chosen on the frame where the wheel visibly moves.  No interpolation
    between the two — a frame is 33 ms, so there is nothing for it to win.

    `silence_s` is the span of the quiet samples themselves — first to last, so
    one sample interval short of the rest an operator would time with a stopwatch
    (20 ms at 50 Hz), which is how the windows were reported when the rule was
    measured.  It is what makes the proposition readable: "4.5 s of silence here,
    first movement there" shows at a glance whether the rule picked the wrong
    rest rather than the wrong threshold.  A stretch the clock could not trust
    contributes nothing to it — `TimeBase` holds its timeline across a dropout —
    so a silence is only ever as long as the time that demonstrably passed.
    """
    candidats = []
    quiet_from = quiet_to = None

    for t_s, omega in samples:
        if omega < threshold:
            if quiet_from is None:
                quiet_from = t_s
            quiet_to = t_s
            continue
        if quiet_from is not None and quiet_to - quiet_from >= min_silence_s:
            candidats.append({
                "t_s":       t_s,
                "silence_s": round(quiet_to - quiet_from, _T_DIGITS),
            })
        quiet_from = quiet_to = None

    motif = None
    if not candidats:
        # The series *is* the gyro stream, so an empty one is the absence of a
        # stream and not a rule that came up empty — which is why this half can
        # tell the two apart without knowing anything about the file it came
        # from, and why `propose([])` is a meaningful call.
        motif = NO_ONSET if len(samples) else NO_GYRO
    return {"candidats": candidats, "motif": motif}


# ── Reading the take ────────────────────────────────────────────────────────

def read_gyro_norm(csv_path: str) -> tuple[list[tuple[float, float]], float]:
    """
    A take's raw gyro-norm curve and the take's duration, from one pass.

    Blocking and I/O-bound — call it through `asyncio.to_thread`, since the loop
    it would otherwise run on is the one owning `processing_loop`, which can
    never drop a packet.

    The duration is the take's, not the curve's: a take whose gyro slot was off
    still has a length, and reporting the curve's last sample instead would say
    zero — a different, false statement (take 001 again).

    `t` is take time on **the same timeline a pose track is stamped with**, so
    the alignment page can draw the curve and the wheel against one cursor. That
    is not a coincidence to be maintained by hand: rows go through
    `playback_engine.row_to_packet`, the CSV's only decoder, and the clock
    advances on exactly the packets a replay would deliver to the model, with
    the same `max_gap_us`.  A row the decoder skips is a row the model would
    never have seen.

    **One source owns the curve**, the first that yields a sample.  An ESP
    configured with a super slot *and* the same sensor as a simple slot sends
    everything twice (see `model/quantities.py`), and merging the two would
    interleave two streams into one series: a sample from either would end a
    silence, and the samples would come in pairs with a wildly irregular step.
    First-wins rather than the resolver's preference table, deliberately — that
    table exists to keep attitude and gyro sampled together, which is not a
    question this asks, and importing the arbitration would put a model concern
    back in the middle of an alignment (ADR 0001).
    """
    clock   = TimeBase(int(config.MAX_DT_S * 1e6))
    samples: list[tuple[float, float]] = []
    source: int | None = None

    with open(csv_path, newline="") as f:
        for row in csv.DictReader(f):
            packet = row_to_packet(row)
            if packet is None:
                continue

            t_us  = clock.update(packet["ts_esp_us"]).t_us
            omega = _gyro_norm(packet)
            if omega is None:
                continue
            if source is None:
                source = packet["typeId"]
            elif packet["typeId"] != source:
                continue

            samples.append((round(t_us / 1e6, _T_DIGITS),
                            round(omega, _V_DIGITS)))

    return samples, round(clock.t_us / 1e6, _T_DIGITS)


def _gyro_norm(packet: dict) -> float | None:
    """|ω| from whichever columns this packet's slot filed it under, or None."""
    fields = _GYRO_FIELDS.get(packet["typeId"])
    if fields is None:
        return None
    try:
        x, y, z = (packet[name] for name in fields)
    except KeyError:
        # A super slot whose deps do not include the gyro: the columns are in
        # the file, blank, and `row_to_packet` leaves them out.
        return None
    return math.sqrt(x * x + y * y + z * z)


def onset_report(csv_path: str) -> dict:
    """
    What `GET …/onset` answers: the proposition, its motif, the whole curve and
    the take's duration.

    One reply rather than two endpoints because the only consumer — the
    alignment page — always wants both: an instant with no curve beside it
    cannot be judged, and a curve with no proposition is the work the page
    exists to avoid.  Blocking, like `read_gyro_norm`.

    `courbe` is a dict of named channels, never a bare array: adding the
    attitude later is one more key, not a change of contract.
    """
    samples, duration_s = read_gyro_norm(csv_path)
    return {
        **propose(samples),
        "courbe":  {"gyro_norm": samples},
        "duree_s": duration_s,
    }
