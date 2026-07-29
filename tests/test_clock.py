"""
tests/test_clock.py — The 32-bit rollover, and everything that looks like it.

The ESP's micros() wraps every 71 min 35 s.  This is the module that has to get
that right, and the one case that is expensive to discover during a show.
"""

from model.clock import TimeBase, Tick, FIRST, OK, WRAP, DISCONTINUITY

WRAP_AT = 1 << 32


def test_first_sample_anchors_the_timeline():
    tb = TimeBase()
    t = tb.update(123_456_789)
    assert t.status == FIRST
    assert t.t_us == 0
    assert t.dt_us is None
    assert not t.integrable


def test_normal_steps_accumulate():
    tb = TimeBase()
    tb.update(1_000_000)
    a = tb.update(1_010_000)
    b = tb.update(1_020_000)
    assert (a.status, a.dt_us, a.t_us) == (OK, 10_000, 10_000)
    assert (b.status, b.dt_us, b.t_us) == (OK, 10_000, 20_000)
    assert b.dt_s == 0.01


def test_rollover_is_real_elapsed_time():
    """The counter going 2**32-5000 → 5000 is a 10 ms step, not a jump back."""
    tb = TimeBase()
    tb.update(WRAP_AT - 5_000)
    t = tb.update(5_000)                     # (2**32 - 5000 + 10000) mod 2**32
    assert t.status == WRAP
    assert t.dt_us == 10_000
    assert t.t_us == 10_000
    assert tb.wraps == 1
    # and the timeline carries on normally afterwards
    t2 = tb.update(15_000)
    assert (t2.status, t2.t_us) == (OK, 20_000)


def test_reboot_from_high_uptime_is_not_mistaken_for_a_rollover():
    """
    The trap: a reboot at 60 min uptime also sends the counter far backwards.

    Magnitude alone cannot tell it from a rollover — unwrapping it would yield a
    695 s "step". Plausibility can: the step is rejected as a discontinuity.
    """
    tb = TimeBase()
    tb.update(3_600_000_000)                 # 60 min of uptime
    t = tb.update(0)                         # power cycle
    assert t.status == DISCONTINUITY
    assert t.dt_us is None
    assert tb.wraps == 0
    assert tb.discontinuities == 1


def test_reboot_from_low_uptime_is_a_discontinuity():
    tb = TimeBase()
    tb.update(1_200_000_000)                 # 20 min of uptime
    t = tb.update(0)
    assert t.status == DISCONTINUITY
    assert tb.wraps == 0


def test_long_dropout_is_not_integrated():
    tb = TimeBase()
    tb.update(1_000_000)
    t = tb.update(1_000_000 + 2_000_000)     # 2 s gap, above max_gap
    assert t.status == DISCONTINUITY
    assert t.dt_us is None


def test_reordered_datagram_is_a_discontinuity():
    tb = TimeBase()
    tb.update(1_000_000)
    tb.update(1_010_000)
    t = tb.update(1_005_000)                 # arrived out of order
    assert t.status == DISCONTINUITY


def test_timeline_never_goes_backwards():
    tb = TimeBase()
    raws = [1_000_000, 1_010_000, 500_000, 1_020_000, 0, WRAP_AT - 1, 100]
    last = -1
    for raw in raws:
        t = tb.update(raw)
        assert t.t_us >= last, f"timeline went backwards at raw={raw}"
        last = t.t_us


def test_resynchronises_after_a_discontinuity():
    """A break must cost one sample, not the rest of the run."""
    tb = TimeBase()
    tb.update(1_000_000)
    tb.update(0)                             # reboot
    a = tb.update(10_000)                    # next sample from the new time base
    assert a.status == OK
    assert a.dt_us == 10_000


def test_long_run_crosses_the_boundary_cleanly():
    """
    The whole point, end to end: 75 minutes at 100 Hz, starting 30 s before a
    rollover.  The measured duration must match the real one to the microsecond,
    with no discontinuity at all.

    Note it wraps *twice*: the period is 4294.97 s, so a 4500 s run starting 30 s
    before a boundary crosses it at t=30 s and again at t=4325 s.  Deriving the
    count rather than hardcoding it is also a check that the period is what we
    think it is.
    """
    step_us   = 10_000                       # 100 Hz
    n         = 75 * 60 * 100                # 75 min
    start_raw = WRAP_AT - 30_000_000         # 30 s before the boundary

    tb = TimeBase()
    tb.update(start_raw)
    for i in range(1, n):
        tb.update((start_raw + i * step_us) % WRAP_AT)

    expected_wraps = (start_raw + (n - 1) * step_us) // WRAP_AT
    assert expected_wraps == 2, "sanity: this run should straddle two boundaries"
    assert tb.wraps == expected_wraps, f"expected {expected_wraps}, got {tb.wraps}"
    assert tb.discontinuities == 0, f"{tb.discontinuities} spurious break(s)"
    assert tb.t_us == (n - 1) * step_us
    assert abs(tb.t_us / 1e6 - 4499.99) < 0.02


def test_reset_forgets_everything():
    tb = TimeBase()
    tb.update(1_000_000)
    tb.update(1_010_000)
    tb.reset()
    t = tb.update(999_999_999)
    assert (t.status, t.t_us, tb.samples) == (FIRST, 0, 1)


def main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")


if __name__ == "__main__":
    main()
