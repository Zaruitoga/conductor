"""
tests/test_scope.py — The history must keep what a detector would trigger on.

A scope that decimates is worse than no scope: it shows a clean trace of a signal
that was never clean, and a threshold set against it fires on nothing.  The one
property worth testing is therefore that a single-sample spike survives being
reduced to a few hundred pixel columns.
"""

import math

import numpy as np

from model.scope import ScopeRing
from model.types import FRAME, META, Frame, Meta


def _frame(t_us, **signals):
    return Frame(t_us=t_us, seq=t_us // 10_000, signals=signals)


def _fill(ring, n, hz=100.0, fn=lambda i: float(i), name="v"):
    step = int(1e6 / hz)
    for i in range(n):
        ring.push(_frame(i * step, **{name: fn(i)}))
    return ring


def test_a_one_sample_spike_survives_the_reduction():
    """
    24 000 samples into 600 columns is 40 samples per column. Averaging would
    divide a spike by forty; the envelope keeps it whole — which is the whole
    reason a detection can be tuned against this.
    """
    ring = ScopeRing(capacity=24_000)
    _fill(ring, 24_000, fn=lambda i: 100.0 if i == 12_345 else 1.0)

    out = ring.history(["v"], window_s=1e6, points=600)
    peak = max(v for v in out["signals"]["v"]["max"] if v is not None)

    assert peak == 100.0, f"the spike was smoothed away: {peak}"
    # And the floor is still visible, so the trace reads as a spike on a baseline
    # rather than as a step.
    assert min(v for v in out["signals"]["v"]["min"] if v is not None) == 1.0


def test_the_envelope_brackets_the_signal():
    """
    The reduction must lose no extremum and invent none.

    Checked against the true extremes of the samples fed in, not against the
    analytic ±1 of the sine: sampled at integer steps it never quite reaches
    them, and asserting the ideal would be testing the generator, not the ring.
    """
    def shape(i):
        return math.sin(i / 50.0)

    ring = ScopeRing(capacity=5_000)
    _fill(ring, 5_000, fn=shape)
    truth = [shape(i) for i in range(5_000)]

    out = ring.history(["v"], window_s=1e6, points=200)
    lo = [v for v in out["signals"]["v"]["min"] if v is not None]
    hi = [v for v in out["signals"]["v"]["max"] if v is not None]

    assert len(lo) == len(hi) == 200
    assert all(a <= b for a, b in zip(lo, hi))
    assert min(lo) == min(truth)
    assert max(hi) == max(truth)


def test_the_window_selects_the_most_recent_stretch():
    ring = ScopeRing(capacity=10_000)
    _fill(ring, 1_000, hz=100.0, fn=lambda i: float(i))    # 10 s of data

    out = ring.history(["v"], window_s=2.0, points=100)

    assert abs(out["t1"] - 9.99) < 1e-6
    assert abs(out["t0"] - 7.99) < 1e-6
    # Closed at both ends, so the window holds samples 799..999 — 201 of them,
    # not 200. Worth pinning: an off-by-one here shifts every trace by a sample.
    assert out["samples"] == 201
    assert max(v for v in out["signals"]["v"]["max"] if v is not None) == 999.0
    assert min(v for v in out["signals"]["v"]["min"] if v is not None) == 799.0


def test_the_ring_wraps_and_keeps_the_newest():
    ring = ScopeRing(capacity=100)
    _fill(ring, 250, fn=lambda i: float(i))

    out = ring.history(["v"], window_s=1e6, points=50)
    values = [v for v in out["signals"]["v"]["max"] if v is not None]

    assert ring.stats()["held"] == 100
    assert max(values) == 249.0
    assert min(v for v in out["signals"]["v"]["min"] if v is not None) == 150.0


def test_gaps_stay_gaps():
    """
    A signal that could not be computed must leave a hole, not a zero.

    Dragging the trace to zero would look exactly like the wheel coming to rest,
    which is a state a detector is meant to recognise.
    """
    ring = ScopeRing(capacity=1_000)
    for i in range(1_000):
        value = None if 400 <= i < 600 else 5.0
        ring.push(_frame(i * 10_000, v=value))

    out = ring.history(["v"], window_s=1e6, points=100)
    holes = [i for i, v in enumerate(out["signals"]["v"]["max"]) if v is None]

    # The gap is 200 of 1000 samples, so a fifth of the 100 columns.
    assert 18 <= len(holes) <= 22, f"expected ~20 empty columns, got {len(holes)}"
    assert 38 <= holes[0] <= 42 and 58 <= holes[-1] <= 62, "the hole moved"
    assert all(v == 5.0 for v in out["signals"]["v"]["max"] if v is not None)


def test_a_signal_appearing_late_has_no_invented_past():
    """Plugging a sensor in mid-session must not backfill zeros before it."""
    ring = ScopeRing(capacity=1_000)
    for i in range(500):
        ring.push(_frame(i * 10_000, lean_deg=10.0))
    for i in range(500, 1_000):
        ring.push(_frame(i * 10_000, lean_deg=10.0, accel_norm_ms2=9.8))

    out = ring.history(["accel_norm_ms2"], window_s=1e6, points=100)
    accel = out["signals"]["accel_norm_ms2"]["max"]

    assert all(v is None for v in accel[:45])
    assert all(v == 9.8 for v in accel[55:])


def test_a_model_reset_empties_the_ring():
    """
    A replay restarts the timeline at zero. Keeping the old samples would make
    the timestamps non-monotonic, and every windowed query nonsense.
    """
    ring = ScopeRing(capacity=1_000)
    _fill(ring, 500)
    assert ring.stats()["held"] == 500

    ring.on_bus(META, Meta(t_us=0, topic="reset"))
    assert ring.stats()["held"] == 0

    ring.on_bus(FRAME, _frame(0, v=1.0))
    assert ring.stats()["held"] == 1


def test_an_empty_ring_answers_without_raising():
    out = ScopeRing(capacity=100).history(["v"], window_s=10.0, points=100)
    assert out["signals"] == {}
    assert out["points"] == 0


def test_a_full_query_is_fast_enough_to_poll():
    """
    The panel polls this several times a second while the model runs at 400 Hz.
    Reducing must stay vectorised: a Python loop over columns would cost tens of
    milliseconds per request and show up as a stuttering panel.
    """
    import time

    ring = ScopeRing(capacity=24_000)
    for i in range(24_000):
        ring.push(_frame(i * 2_500, **{f"s{k}": float(i + k) for k in range(20)}))

    names = [f"s{k}" for k in range(6)]
    t0 = time.perf_counter()
    for _ in range(10):
        ring.history(names, window_s=60.0, points=600)
    per_call_ms = (time.perf_counter() - t0) * 100

    assert per_call_ms < 25.0, f"{per_call_ms:.1f} ms per query is too slow to poll"


def main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")


if __name__ == "__main__":
    main()
