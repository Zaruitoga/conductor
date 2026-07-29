"""
model/clock.py — The model's single time source.

The ESP stamps every packet with `micros()`, a **32-bit** counter
(`DataHeader` is `<BBHII`, see transport/protocol.py).  It therefore wraps back
to zero every 2**32 µs = **71 min 35 s**.  A session longer than that, or a
recording that straddles the boundary, produces a timeline that jumps backwards
by 71 minutes in the middle.

`TimeBase` is the one place that problem is solved.  It unwraps the counter into
a monotonic 64-bit µs timeline, relative to the first sample it saw, and reports
for each sample whether the elapsed time since the previous one is trustworthy.

**The rule the whole model depends on:** every time-dependent computation reads
`Tick.t_us` / `Tick.dt_us` and nothing else — never `time.monotonic()`, never
`ts_rx_us` (wall clock), never the asyncio loop clock.  That is what makes a
replay reproduce a live run exactly, and a 4× replay reproduce a 1× replay
exactly.

Telling a wrap from a reboot
----------------------------
Both look like the counter going backwards, and they need opposite handling: a
wrap is real elapsed time that must be added, a reboot is a broken timeline that
must not be integrated.  They are separated by *plausibility* rather than by
magnitude alone: a candidate wrap is only accepted when the unwrapped step is a
credible inter-packet interval (≤ `max_gap_us`).  A reboot from a high uptime
also produces a large negative delta, but unwrapping it would yield a step of
minutes — rejected, and treated as a discontinuity.

Discontinuities do not advance the timeline
-------------------------------------------
When the step is not trustworthy (reboot, out-of-order datagram, long dropout),
`t_us` is held and `dt_us` is None: the sample is still delivered, but nothing
integrates it.  Holding rather than guessing keeps the timeline monotonic *and*
deterministic — inventing an advance from the wall clock is exactly the kind of
thing that would make a replay disagree with the run it replays.
"""

from dataclasses import dataclass

# The ESP counter is uint32: it counts to 2**32 - 1 µs and rolls over.
_WRAP = 1 << 32
_HALF = 1 << 31

# Sample classification, carried on every Tick.
FIRST         = "first"          # nothing to compare against yet
OK            = "ok"             # normal forward step
WRAP          = "wrap"           # forward step that crossed the 32-bit boundary
DISCONTINUITY = "discontinuity"  # reboot, reordering, or a gap too long to trust


@dataclass(frozen=True, slots=True)
class Tick:
    """
    One sample placed on the model's timeline.

    t_us   monotonic µs since the first sample (never decreases)
    dt_us  µs since the previous sample, or None when it must not be integrated
    status one of FIRST / OK / WRAP / DISCONTINUITY
    """
    t_us:   int
    dt_us:  int | None
    status: str

    @property
    def dt_s(self) -> float:
        """Elapsed seconds, or 0.0 when the step is not trustworthy."""
        return 0.0 if self.dt_us is None else self.dt_us / 1e6

    @property
    def t_s(self) -> float:
        return self.t_us / 1e6

    @property
    def integrable(self) -> bool:
        """True when this sample carries a usable elapsed time."""
        return self.dt_us is not None and self.dt_us > 0


class TimeBase:
    """
    Unwraps the ESP's 32-bit `micros()` into a monotonic 64-bit timeline.

    Stateful and not thread-safe: one instance per stream being interpreted,
    driven from a single task.  `reset()` returns it to its initial state and
    must be called whenever the source changes (start of a replay pass, return
    to live), exactly like a stateful model node.
    """

    def __init__(self, max_gap_us: int = 500_000):
        """
        max_gap_us is the longest inter-sample interval still treated as real
        elapsed time.  Above it the value is a broken timeline rather than
        motion — 0.5 s is ~50 missed packets at 100 Hz, a gap that is not worth
        integrating blind anyway.  It doubles as the plausibility bound that
        separates a wrap from a reboot.
        """
        self._max_gap_us = int(max_gap_us)
        self.reset()

    def reset(self) -> None:
        """Forget the timeline. The next sample becomes the new origin."""
        self._last_raw: int | None = None
        self.t_us:      int = 0
        # Counters, surfaced in the panel: seeing "wraps: 1" after 72 minutes is
        # how you confirm the rollover was handled instead of wondering.
        self.samples:        int = 0
        self.wraps:          int = 0
        self.discontinuities: int = 0

    def update(self, ts_esp_us: int) -> Tick:
        """
        Place one raw ESP timestamp on the timeline.

        Always returns a Tick — a sample is never rejected here.  Deciding what
        to do with an untrustworthy step belongs to the model nodes, which read
        `Tick.integrable`; dropping it here would hide the discontinuity from
        the very code that needs to know about it.
        """
        raw = int(ts_esp_us) & 0xFFFFFFFF
        self.samples += 1

        if self._last_raw is None:
            self._last_raw = raw
            self.t_us = 0
            return Tick(0, None, FIRST)

        delta = raw - self._last_raw
        status = OK

        if delta < -_HALF:
            # Counter went far backwards: candidate rollover. Accepting it is
            # conditional on the unwrapped step being plausible (below).
            delta += _WRAP
            status = WRAP

        if 0 <= delta <= self._max_gap_us:
            self._last_raw = raw
            self.t_us += delta
            if status is WRAP:
                self.wraps += 1
            return Tick(self.t_us, delta, status)

        # Not a credible step: ESP reboot, a datagram that overtook its
        # predecessor, or a dropout long enough that the interval is meaningless.
        # Re-anchor on the new value so the *next* sample resynchronises, but
        # hold the timeline: we do not know how much time actually passed, and
        # any guess would have to come from a clock the replay does not share.
        self._last_raw = raw
        self.discontinuities += 1
        return Tick(self.t_us, None, DISCONTINUITY)

    def stats(self) -> dict:
        """Counters for the panel snapshot."""
        return {
            "t_us":             self.t_us,
            "samples":          self.samples,
            "wraps":            self.wraps,
            "discontinuities":  self.discontinuities,
        }
