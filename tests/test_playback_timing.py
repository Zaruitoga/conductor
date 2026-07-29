"""
tests/test_playback_timing.py — A take longer than 71 minutes must still replay.

The pacing clock reads the same uint32 the model does, so it inherits the same
rollover.  Left alone, every row after the boundary produced a negative offset:
the replay waited zero and flushed the whole tail in one burst, and the take
reported a negative duration.

Only the timeline arithmetic is exercised here — the replay loop's own pacing is
asyncio sleeps, which are not what this can get wrong.
"""

from model.clock import TimeBase
from storage.playback_engine import _PACING_MAX_GAP_US

WRAP_AT = 1 << 32


def _offsets(raw_timestamps):
    """The take-relative timeline PlaybackEngine builds from a take's rows."""
    clock = TimeBase(_PACING_MAX_GAP_US)
    return [clock.update(ts).t_us for ts in raw_timestamps], clock


def test_a_take_crossing_the_rollover_keeps_moving_forward():
    step = 10_000                                  # 100 Hz
    start = WRAP_AT - 20 * step                    # 0.2 s before the boundary
    raw = [(start + i * step) % WRAP_AT for i in range(200)]

    offsets, clock = _offsets(raw)

    assert clock.wraps == 1
    assert offsets == sorted(offsets), "the timeline went backwards mid-take"
    assert all(b - a == step for a, b in zip(offsets, offsets[1:]))
    # Before the fix this duration was negative, and every row past the boundary
    # was already "due", so the tail replayed instantly.
    assert offsets[-1] / 1e6 == (len(raw) - 1) * step / 1e6


def test_duration_of_a_long_take_is_right():
    """75 minutes at 100 Hz: the reported duration must be 75 minutes."""
    step = 10_000
    n = 75 * 60 * 100
    start = WRAP_AT - 30_000_000
    raw = [(start + i * step) % WRAP_AT for i in range(n)]

    offsets, clock = _offsets(raw)

    assert clock.wraps == 2                        # 75 min spans two periods
    assert abs(offsets[-1] / 1e6 - 4499.99) < 0.02


def test_a_real_dropout_inside_a_take_is_preserved():
    """
    The pacing tolerance is looser than the model's on purpose: a one-second hole
    in a recording is real elapsed time and should still be waited out, not
    collapsed. Only an implausible jump is treated as a broken timeline.
    """
    step = 10_000
    raw = [i * step for i in range(50)]
    raw += [raw[-1] + 1_000_000 + i * step for i in range(50)]   # 1 s gap

    offsets, clock = _offsets(raw)

    assert clock.discontinuities == 0
    assert offsets[50] - offsets[49] == 1_000_000


def main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")


if __name__ == "__main__":
    main()
