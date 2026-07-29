"""
model/scope.py — Full-rate signal history, so a threshold can be set by looking.

The panel snapshot runs at 4 Hz.  At 100 Hz that is one sample in twenty-five,
and at 4× replay one in a hundred: enough to show a number, useless for deciding
where a detection should fire.  Whatever you were trying to catch happened
between two samples.

So the history lives here, backend-side, fed inline from the bus at the model's
own rate.  Being an inline subscriber matters: it means the ring sees *every*
frame, including during a fast replay, where the WebSocket fan-out is dropping
frames on purpose to keep a browser current.

Min/max envelopes, not decimation
---------------------------------
A 60 s window at 400 Hz is 24 000 samples; a scope trace is ~600 pixels wide.
Sending every 40th sample would smooth away exactly what a detector triggers
on — a single-sample spike is a real impact, not noise to be averaged out.  So
each pixel column is returned as the *minimum and maximum* over its time bucket:
the trace shows the true excursion, and a peak one sample wide is still visibly
a peak.

Storage
-------
One preallocated float64 ring per signal, plus one shared timestamp ring, rather
than a ring of frames: 24 000 frames of dicts is tens of megabytes and slow to
query, while the same in arrays is a few and queries vectorise.  Missing values
are NaN, and the envelope reduction uses fmin/fmax, which skip them — so a
signal that could not be computed for a while leaves a gap in the trace instead
of dragging it to zero.
"""

import logging

import numpy as np

from model.types import FRAME, META

log = logging.getLogger("model.scope")

# Samples kept per signal. Sizing in samples rather than seconds is deliberate:
# what needs to survive is a fixed number of *points* to draw. 24 000 is 60 s at
# 400 Hz, 4 min at 100 Hz, and 16 min at 25 Hz — always at least the minute the
# eye needs, and more when the stream is slow enough to afford it.
DEFAULT_CAPACITY = 24_000

# Upper bound on what one request may return, so a stray `points=1000000` cannot
# turn a poll into a stall.
MAX_POINTS = 4_000


class ScopeRing:
    """
    Rolling full-rate history of every signal the model publishes.

    Written inline from the bus (single task, no locking) and read from route
    handlers on the same loop.
    """

    def __init__(self, capacity: int = DEFAULT_CAPACITY):
        self._capacity = int(capacity)
        self.clear()

    # ── Writing ──────────────────────────────────────────────────────────────

    def clear(self) -> None:
        """Drop the whole history. Called whenever the timeline restarts."""
        self._t = np.zeros(self._capacity, dtype=np.int64)
        self._v: dict[str, np.ndarray] = {}
        self._head  = 0        # next slot to write
        self._count = 0        # samples held, capped at capacity
        self.pushed = 0

    def on_bus(self, kind: str, obj) -> None:
        """Bus handler. Frames are recorded; a model reset empties the ring."""
        if kind == FRAME:
            self.push(obj)
        elif kind == META and getattr(obj, "topic", None) == "reset":
            # The timeline restarts at zero, so the timestamps would stop being
            # monotonic and every windowed query would return nonsense.
            self.clear()

    def push(self, frame) -> None:
        i = self._head
        self._t[i] = frame.t_us

        for name, value in frame.signals.items():
            column = self._v.get(name)
            if column is None:
                # A signal that just became available has no past: fill it with
                # NaN so its trace starts where it started, rather than at zero.
                column = np.full(self._capacity, np.nan, dtype=np.float64)
                self._v[name] = column
            column[i] = np.nan if value is None else value

        # Signals that disappeared this tick (sensor unplugged) get a NaN, so
        # their trace stops instead of holding its last value forever.
        if len(self._v) != len(frame.signals):
            for name, column in self._v.items():
                if name not in frame.signals:
                    column[i] = np.nan

        self._head  = (i + 1) % self._capacity
        self._count = min(self._count + 1, self._capacity)
        self.pushed += 1

    # ── Reading ──────────────────────────────────────────────────────────────

    @property
    def names(self) -> list[str]:
        return sorted(self._v)

    def _ordered(self):
        """The held samples in chronological order: (timestamps, index array)."""
        if self._count == 0:
            return np.empty(0, dtype=np.int64), np.empty(0, dtype=np.intp)
        if self._count < self._capacity:
            idx = np.arange(self._count, dtype=np.intp)
        else:
            # Full ring: the oldest sample sits just after the write head.
            idx = (np.arange(self._capacity, dtype=np.intp) + self._head) % self._capacity
        return self._t[idx], idx

    def span(self) -> tuple[float, float] | None:
        """(first, last) timestamps held, in seconds, or None when empty."""
        t, _ = self._ordered()
        if t.size == 0:
            return None
        return float(t[0]) / 1e6, float(t[-1]) / 1e6

    def history(self, names, window_s: float = 10.0,
                points: int = 600) -> dict:
        """
        Min/max envelope of each named signal over the last `window_s` seconds.

        Returns `{t0, t1, points, dt, signals: {name: {min: [...], max: [...]}}}`
        with `None` in place of NaN so it survives JSON.
        """
        points = max(1, min(int(points), MAX_POINTS))
        t, idx = self._ordered()

        if t.size == 0:
            return {"t0": 0.0, "t1": 0.0, "points": 0, "dt": 0.0, "signals": {}}

        t1_us = int(t[-1])
        t0_us = max(int(t[0]), t1_us - int(window_s * 1e6))

        start = int(np.searchsorted(t, t0_us, side="left"))
        t_win = t[start:]
        idx_win = idx[start:]
        if t_win.size == 0:
            return {"t0": t0_us / 1e6, "t1": t1_us / 1e6,
                    "points": 0, "dt": 0.0, "signals": {}}

        # Bucket boundaries, one per output column.
        edges = np.linspace(t0_us, t1_us + 1, points + 1)
        starts = np.searchsorted(t_win, edges[:-1], side="left")
        ends   = np.searchsorted(t_win, edges[1:],  side="left")
        filled = ends > starts

        out = {}
        for name in names:
            column = self._v.get(name)
            if column is None:
                continue
            out[name] = self._envelope(column[idx_win], starts, filled, points)

        return {
            "t0":      t0_us / 1e6,
            "t1":      t1_us / 1e6,
            "points":  points,
            "dt":      (t1_us - t0_us) / 1e6 / points,
            "samples": int(t_win.size),
            "signals": out,
        }

    @staticmethod
    def _envelope(values, starts, filled, points: int) -> dict:
        """
        Reduce one signal to per-column minima and maxima.

        `fmin`/`fmax` rather than `minimum`/`maximum`: they ignore NaN, so a
        stretch where the signal could not be computed leaves a hole in the trace
        instead of poisoning the whole column.

        `reduceat` segments run from one start index to the next, which spans any
        empty buckets in between — harmless, since an empty bucket contributes no
        samples — so the vectorised call is correct without special-casing gaps.
        """
        mins = np.full(points, np.nan)
        maxs = np.full(points, np.nan)
        used = starts[filled]
        if used.size:
            mins[filled] = np.fmin.reduceat(values, used)
            maxs[filled] = np.fmax.reduceat(values, used)
        return {
            "min": [None if np.isnan(v) else float(v) for v in mins],
            "max": [None if np.isnan(v) else float(v) for v in maxs],
        }

    def stats(self) -> dict:
        span = self.span()
        return {
            "capacity": self._capacity,
            "held":     self._count,
            "pushed":   self.pushed,
            "seconds":  0.0 if span is None else round(span[1] - span[0], 2),
            "signals":  len(self._v),
        }
