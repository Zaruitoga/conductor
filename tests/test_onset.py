"""
tests/test_onset.py — The proposed start of movement, and the two constants it rests on.

The proposition never has authority (ADR 0001): a human confirms the pair of
anchors, and the detection may be retuned forever without moving one already
confirmed.  What it must nevertheless be is *readable* — an operator judges it
in a glance from the curve and the rest that precedes it — and that is what
these tests pin down:

  * the anchor is the first sample **after** the silence, never the last one
    before it.  Half a second is invisible at the anchor point and glaring
    thirty seconds later, and one sample is 20 ms at the rate the reference
    session recorded at;
  * **every** candidate comes back, in order.  The pattern happens two to four
    times per take (the wheel is set down again), and the nuance that makes the
    rule work is that the *first* rest is the one that counts, not the best one
    — so the caller has to see the others to overrule it;
  * "no gyro stream at all" is not "nothing detected".  Take 001 of the
    reference session is a real specimen: 171 rows, `GAME_RV` only.  That
    distinction carries the whole degraded state of the alignment page;
  * the rule does not move across the range of thresholds and durations that
    were considered — which is what allowed the two constants not to be made
    tunable, and therefore what has to stay true.

The one place a value genuinely matters is the tremble: take 004 shows the
wheel stirring between 0.02 and 0.29 rad/s for 580 ms before the real gesture.
At 0.5 rad/s that stays inside the silence; at 0.15 it ends it, and the anchor
lands 14 frames early with nothing to say so.  `test_a_tremble_…` is that take,
in miniature.

The pure half is tested on synthetic `(t, |ω|)` series and the reading half on
takes written by the real `CSVLogger`, because the trap on that side is a column
layout: a simple `GYRO` slot files its vector under `x,y,z` and a super slot
under `gyro_x…` (issue #12), and the reference session uses the *first*.
"""

import json
import math

import core
from storage.onset import (
    NO_GYRO, NO_ONSET, SILENCE_MAX_RAD_S, SILENCE_MIN_S,
    onset_report, propose, read_gyro_norm,
)

from tests.test_paths import _app, _call
from tests.test_pose_track import _Take

# What the reference session actually recorded at: two simple slots at 50 Hz,
# so one gyro sample is 20 ms against a video frame's 33 ms.
HZ = 50.0


def _curve(*segments) -> list[tuple[float, float]]:
    """
    A `(t, |ω|)` series from `(seconds, value)` segments, sampled at HZ.

    `t` is index/HZ rather than an accumulated sum, so a segment boundary lands
    on an exact sample time and the expected anchors below are readable.
    """
    out: list[tuple[float, float]] = []
    for seconds, value in segments:
        base = len(out)
        out.extend(((base + i) / HZ, float(value))
                   for i in range(round(seconds * HZ)))
    return out


def _at(*seconds) -> list[float]:
    """The anchors a caller reads: `t_s` of each candidate, in order."""
    return [round(s, 6) for s in seconds]


def _anchors(proposition: dict) -> list[float]:
    return [c["t_s"] for c in proposition["candidats"]]


# ── The rule, on a synthetic curve ───────────────────────────────────────────

def test_the_anchor_is_the_first_sample_after_the_silence():
    """
    The wheel is still, then it is not: the anchor is that second fact.  Naming
    the last quiet sample instead would put the anchor one sample early, every
    time, in the same direction — a bias, not a rounding error.
    """
    proposition = propose(_curve((3, 0.0), (2, 8.0)))

    assert _anchors(proposition) == _at(3.0)
    assert proposition["motif"] is None
    # The rest that carries it: the quiet run itself, 0.00 → 2.98.
    assert proposition["candidats"][0]["silence_s"] == 149 / HZ


def test_every_candidate_comes_back_in_order_and_the_first_is_the_rule_s():
    """
    The wheel is set down two to four times per take, and the rule designates
    the *first* rest — which was right on one take in three until that nuance
    was added.  Overruling it is a closed choice between candidates rather than
    free pointing, so the others have to be there.
    """
    proposition = propose(_curve(
        (3,   0.0), (1, 8.0),     # a rest long enough  → 3.0
        (2.5, 0.0), (1, 8.0),     # another one         → 6.5
        (0.5, 0.0), (1, 8.0),     # too short           → nothing
    ))

    assert _anchors(proposition) == _at(3.0, 6.5)
    assert _anchors(proposition) == sorted(_anchors(proposition))


def test_a_rest_shorter_than_the_rule_is_not_a_rest():
    proposition = propose(_curve((1.5, 0.0), (2, 8.0)))

    assert proposition["candidats"] == []
    assert proposition["motif"] == NO_ONSET


def test_a_take_that_never_rests_says_why_rather_than_going_quiet():
    """An empty list alone would read as a bug in the page displaying it."""
    proposition = propose(_curve((5, 8.0)))

    assert proposition["candidats"] == []
    assert proposition["motif"] == NO_ONSET


def test_a_rest_that_is_never_ended_produces_nothing():
    """
    The rule needs the crossing, not just the silence: a take that ends with the
    wheel on the ground has no start of movement in it.
    """
    proposition = propose(_curve((2, 8.0), (5, 0.0)))

    assert proposition["candidats"] == []
    assert proposition["motif"] == NO_ONSET


def test_no_gyro_at_all_is_not_the_same_fact_as_nothing_detected():
    """
    Take 001 of the reference session — gyro slot switched off, 171 rows of
    `GAME_RV`.  "The method does not apply here" and "the method ran and found
    nothing" send an operator looking in two different places.
    """
    proposition = propose([])

    assert proposition["candidats"] == []
    assert proposition["motif"] == NO_GYRO


# ── What the two constants are worth ─────────────────────────────────────────

def test_the_rule_holds_across_the_thresholds_that_were_not_made_tunable():
    """
    0.15 → 0.5 rad/s and 2 → 3 s: the whole range that was on the table.  If the
    answer moved inside it, these two numbers would have to become parameters —
    and a proposition would then depend on a setting, which is the one thing
    ADR 0001 rules out.

    The curve is built to put something on *both* sides of each swept bound,
    since a sweep over values that cannot straddle it proves nothing: the rest
    sits at 0.1 rad/s (just under the lowest threshold), the movement starts at
    0.7 (over the highest), one rest spans 1.88 s (just under the shortest
    duration) and the others 3.48 s (over the longest).  What would genuinely
    move the answer is a sample *inside* a swept band — which is the tremble
    below, and the reason the range stops at 0.5 rather than going lower.
    """
    curve = _curve((4, 0.1), (0.5, 0.7), (0.5, 9.0),   # a rest, then a firm start
                   (3.5, 0.1), (1, 9.0),               # a second rest, also long
                   (1.9, 0.1), (1, 9.0))               # too short at every duration

    for threshold in (0.15, 0.3, SILENCE_MAX_RAD_S):
        for min_silence_s in (SILENCE_MIN_S, 2.5, 3.0):
            proposition = propose(curve, threshold=threshold,
                                  min_silence_s=min_silence_s)
            assert _anchors(proposition) == _at(4.0, 8.5), \
                f"the rule moved at threshold={threshold}, min={min_silence_s}"


def test_a_tremble_below_the_threshold_does_not_end_the_silence():
    """
    Take 004, in miniature: after an exact zero the wheel stirs at 0.29 rad/s
    for 580 ms before the real gesture.  At 0.5 rad/s — three times the highest
    tremble measured, and what a camera resolves as ~3 px between two frames —
    the anchor is the gesture.  Lower it to 0.15 and the anchor slides 580 ms
    early onto a movement invisible in the image, silently: this is the failure
    the constant is chosen against, so both halves are asserted.
    """
    curve = _curve((4, 0.0), (0.58, 0.29), (2, 9.0))

    assert _anchors(propose(curve)) == _at(4.58), "the tremble ended the silence"
    assert _anchors(propose(curve, threshold=0.15)) == _at(4.0), \
        "the counterfactual no longer bites — the constant has stopped mattering"


def test_the_threshold_is_a_crossing_not_a_ceiling():
    """A sample exactly at the threshold is movement, and the rest below it."""
    curve = _curve((3, 0.0), (1, SILENCE_MAX_RAD_S))

    assert _anchors(propose(curve)) == _at(3.0)


# ── Reading a take: two transports, one timeline ─────────────────────────────

def _gyro_simple(seq, t_s, norm):
    """A simple `GYRO` slot packet — the reference session's own configuration."""
    ts = int(round(t_s * 1e6))
    return {"type": "gyro", "typeId": 0x01, "seq": seq,
            "ts_esp_us": ts, "ts_rx_us": ts,
            "gyro_x": norm / math.sqrt(3), "gyro_y": norm / math.sqrt(3),
            "gyro_z": norm / math.sqrt(3)}


def _gyro_super(seq, t_s, norm):
    """The same vector, bundled — and filed under the very same columns."""
    ts = int(round(t_s * 1e6))
    return {"type": "super_0", "typeId": 0x10, "seq": seq,
            "ts_esp_us": ts, "ts_rx_us": ts, "dep_slots": [0, 6],
            "gyro_x": norm / math.sqrt(3), "gyro_y": norm / math.sqrt(3),
            "gyro_z": norm / math.sqrt(3),
            "game_rv_qw": 1.0, "game_rv_qx": 0.0,
            "game_rv_qy": 0.0, "game_rv_qz": 0.0}


def _attitude(seq, t_s):
    ts = int(round(t_s * 1e6))
    return {"type": "game_rv", "typeId": 0x07, "seq": seq,
            "ts_esp_us": ts, "ts_rx_us": ts,
            "game_rv_qw": 1.0, "game_rv_qx": 0.0,
            "game_rv_qy": 0.0, "game_rv_qz": 0.0}


def _packets(build, *segments, t0=0.0):
    """A take's worth of gyro packets following `_curve`'s segments."""
    return [build(i, t0 + t, v) for i, (t, v) in enumerate(_curve(*segments))]


def test_the_curve_is_read_from_either_transport():
    """
    The same gesture recorded on a simple `GYRO` slot and inside a super slot
    gives the same curve — and this module needs no table of packet types to
    get it, both filing their vector under `gyro_x/y/z` (issue #12).  Before
    that, reading only one of the two dispositions worked perfectly against
    every fixture in this repo and against none of the real takes.
    """
    curves = []
    for build in (_gyro_simple, _gyro_super):
        take = _Take(_packets(build, (3, 0.0), (1, 8.0)))
        try:
            samples, duration_s = read_gyro_norm(take.csv)
            curves.append(samples)
            assert duration_s == round(199 / HZ, 6)
        finally:
            take.close()

    assert curves[0] == curves[1], "the two layouts gave different curves"
    assert _anchors(propose(curves[0])) == _at(3.0)


def test_the_curve_is_on_the_takes_timeline_not_the_gyro_streams():
    """
    `t` here has to be the same take time a pose track is stamped with, since
    the alignment page draws the two against one cursor.  A reader that started
    its own clock on the first *gyro* row would be silently early by however
    long the attitude stream ran alone — and both are one `TimeBase` over the
    rows a replay would deliver.
    """
    warmup = [_attitude(i, i / HZ) for i in range(round(1.0 * HZ))]
    take = _Take(warmup + _packets(_gyro_simple, (3, 0.0), (1, 8.0), t0=1.0))
    try:
        samples, duration_s = read_gyro_norm(take.csv)

        assert samples[0][0] == 1.0, "the gyro curve was rebased to zero"
        assert _anchors(propose(samples)) == _at(4.0)
        assert duration_s == round(1.0 + 199 / HZ, 6)
    finally:
        take.close()


def test_one_source_owns_the_curve_when_the_esp_sends_the_gyro_twice():
    """
    An ESP configured with a super slot *and* the same sensor as a simple slot
    delivers everything twice — normal, and stated as such in `model/`.  Merged,
    the two streams would interleave into one series with a wildly irregular
    step, and a sample from either would end a silence: here the bundled copy
    keeps moving through the rest, so a merged curve has no silence at all and
    proposes nothing.
    """
    quiet, moving = (3, 0.0), (1, 8.0)
    simple = _packets(_gyro_simple, quiet, moving)
    bundled = _packets(_gyro_super, (4, 8.0))            # the same take, still stirring
    interleaved = [p for pair in zip(simple, bundled) for p in pair]

    take = _Take(interleaved)
    try:
        samples, _ = read_gyro_norm(take.csv)

        assert len(samples) == len(simple), "both streams landed in one curve"
        assert _anchors(propose(samples)) == _at(3.0)
    finally:
        take.close()


def test_a_bundle_without_a_gyro_among_its_deps_is_not_a_gyro_stream():
    """
    A super slot carries whichever sensors its dep list names, so its gyro
    columns can be present in the file and blank on every row.  That is "no gyro
    stream" — the reading half must say so rather than raise on the missing
    columns or, worse, read three zeros as a wheel at rest.
    """
    def bundle(seq, t_s, _norm):
        packet = _gyro_super(seq, t_s, 0.0)
        for name in ("gyro_x", "gyro_y", "gyro_z"):
            del packet[name]
        return packet

    take = _Take(_packets(bundle, (3, 0.0), (1, 8.0)))
    try:
        samples, duration_s = read_gyro_norm(take.csv)

        assert samples == []
        assert propose(samples)["motif"] == NO_GYRO
        assert duration_s == round(199 / HZ, 6), "the take still has a length"
    finally:
        take.close()


def test_a_take_with_no_gyro_stream_still_reports_its_duration():
    """
    Take 001 again, end to end.  The page cannot propose anything, but it can
    still show the take's length — and a duration read from the curve would be
    zero here, which is a different (and false) statement.
    """
    take = _Take([_attitude(i, i / HZ) for i in range(round(3.4 * HZ))])
    try:
        report = onset_report(take.csv)

        assert report["candidats"] == []
        assert report["motif"] == NO_GYRO
        assert report["courbe"]["gyro_norm"] == []
        assert report["duree_s"] == round(169 / HZ, 6)
    finally:
        take.close()


# ── The endpoint ─────────────────────────────────────────────────────────────

def _get(path: str) -> tuple[int, dict | str]:
    """
    Drive one GET through the real ASGI app, and decode the body.

    `_call` rather than `TestClient`: an HTTP client would pull `httpx` in, and
    this suite is dependency-free by design — a module that cannot be imported
    without it takes the whole run down on a machine set up from the documented
    install line.  It is also the same door `test_paths` and `test_take_guard`
    knock on, and the only one that can carry the `..` below unnormalised.
    """
    status, text = _call(_app(), "GET", path)
    try:
        return status, json.loads(text)
    except json.JSONDecodeError:
        return status, text


class _Served:
    """A take the routes can reach: `core.session_manager` points at its tree."""

    def __init__(self, packets):
        self.take = _Take(packets)
        self._saved = core.session_manager
        core.session_manager = self.take.sm

    def __enter__(self):
        return self.take

    def __exit__(self, *exc):
        core.session_manager = self._saved
        self.take.close()


def test_the_endpoint_returns_the_proposition_and_the_whole_curve():
    """
    One round trip carries the proposition, its motif and the curve, because the
    only consumer wants all three at once — and the curve goes out **unreduced**
    (ADR 0002).  Verifying an alignment is a zooming activity: with envelopes
    computed server-side every zoom is a round trip at a resolution the server
    picked, and the front a start of movement consists of is a few samples wide.
    """
    spike = 6.0
    packets = _packets(_gyro_simple, (3, 0.0), (0.02, spike), (2.98, 0.0))

    with _Served(packets) as take:
        status, body = _get(f"/api/sessions/{take.session}/takes/{take.take}/onset")
        assert status == 200, body

        assert [c["t_s"] for c in body["candidats"]] == _at(3.0)
        assert body["motif"] is None
        assert abs(body["duree_s"] - 299 / HZ) < 1e-9

        curve = body["courbe"]
        assert set(curve) == {"gyro_norm"}, \
            "the curve is a dict of named channels, never a bare array"
        assert len(curve["gyro_norm"]) == len(packets), "the curve was reduced"
        assert curve["gyro_norm"][150] == [3.0, spike], \
            "the one-sample front is exactly what a reduction would smooth away"


def test_the_endpoint_says_when_the_method_does_not_apply():
    """The degraded state the alignment page renders — a 200, not an error."""
    with _Served([_attitude(i, i / HZ) for i in range(100)]) as take:
        status, body = _get(f"/api/sessions/{take.session}/takes/{take.take}/onset")

        assert status == 200, body
        assert body["motif"] == NO_GYRO
        assert body["candidats"] == [] and body["courbe"]["gyro_norm"] == []
        assert body["duree_s"] > 0


def test_the_endpoint_is_born_confined():
    """
    A third route taking `{session}`/`{take}` (issue #11).  The shape layer is
    declared, so a name that is a path never reaches the handler — and the `..`
    reaches the router unnormalised, which an HTTP client would never let it do.
    """
    for path in ("/api/sessions/../takes/001_a/onset",
                 "/api/sessions/s/takes/../onset",
                 "/api/sessions/s/takes/..%2F../onset"):
        status, body = _get(path)
        assert status == 422, f"GET {path} → {status} {body}"


def test_a_take_that_does_not_exist_is_a_404():
    with _Served([]) as take:
        status, body = _get(
            f"/api/sessions/{take.session}/takes/002_absent/onset")
        assert status == 404, body


def main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")


if __name__ == "__main__":
    main()
