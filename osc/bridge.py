"""
osc/bridge.py — The bus subscriber that turns routes into OSC messages.

Three bus topics, three different behaviours, because model/types.py says they
must not be handled alike:

  FRAME  inline subscribe_sync, *latest frame wins* — no backlog of stale
         values is ever kept. A separate task, on its own real-time cadence,
         reads whatever the latest frame is and sends. This is deliberately
         not the bus's own LOSSY policy: LOSSY still queues (bounded) frames
         between sends, and none of that backlog is wanted here — only "what
         is true right now, at the moment I'm about to send" is.

  EVENT  subscribe(policy=RELIABLE), sent immediately, never rate-capped and
         never deadbanded. A trigger is not a level the model measures every
         tick; missing one is the fault the bus's RELIABLE policy exists to
         prevent (model/bus.py), and this bridge must not reintroduce a loss
         the bus already promised not to have.

  META   inline, only "reset" matters: it clears the deadband memory, so the
         first post-reset value is sent even if it happens to equal the last
         one before the reset — the same reasoning ScopeRing uses to clear its
         ring on the same topic (model/scope.py).

Real time, not model time
--------------------------
The cadenced sender uses `asyncio.sleep`, never `ctx.t_us`. This is not an
exception to "all time comes from Tick.t_us" (CLAUDE.md) — that rule keeps the
*model* reproducible, and nothing here feeds back into it. Live absorbs a
budget of messages per real second regardless of what the model's clock is
doing, so a replay at 4× must send OSC at the same rate a 1× replay would, not
four times as many messages in a quarter of the time.

Never a 0 for "nothing to say"
-------------------------------
A signal that is `None` — unavailable, or computed-but-nothing-to-report —
sends nothing. A wheel that stops is not the same fact as a wheel reporting
zero speed, and collapsing the two would make a detector on the Live end
unable to tell a real zero from silence.
"""

import asyncio
import logging
import time
from collections import deque

from model.bus import RELIABLE
from model.types import EVENT, FRAME, META
from osc.routes import KIND_EVENT, KIND_SIGNAL, Route, RouteTable

log = logging.getLogger("osc.bridge")

DEFAULT_RATE_HZ = 30.0

# Sending window used for the overall out-Hz figure, same idiom and window as
# transport/live_monitor.py's own rate tracking.
_RATE_WINDOW_S = 1.0


def read_source(frame, source: str):
    """A frame's value for `source` — a signal name, or `pose.<field>`."""
    if source.startswith("pose."):
        return frame.pose.get(source[len("pose."):])
    return frame.signals.get(source)


def transform(route: Route, value: float) -> float:
    """
    `in_min..in_max` → `out_min..out_max`, clamped and/or inverted.

    `in_min == in_max` cannot come from `create`/`update` (osc/routes.py
    refuses it), but a tolerantly-loaded profile can still carry one — the
    guard below is what stands between that and a division by zero.
    """
    span = route.in_max - route.in_min
    t = 0.0 if span == 0 else (value - route.in_min) / span
    if route.clamp:
        t = max(0.0, min(1.0, t))
    if route.invert:
        t = 1.0 - t
    return route.out_min + t * (route.out_max - route.out_min)


class OscBridge:
    """Reads routes, watches the bus, sends OSC. Owns no socket of its own —
    that is `live` (osc/live.py), handed in so discovery and health can be
    driven independently of whether the bridge itself is enabled."""

    def __init__(self, routes: RouteTable, live, rate_hz: float = DEFAULT_RATE_HZ):
        self.routes  = routes
        self.live    = live
        self.enabled = True
        self.rate_hz = rate_hz

        self._latest_frame = None
        self._last_value:   dict[str, float | None] = {}
        self._last_sent_at: dict[str, float] = {}
        self._send_times: deque = deque()      # monotonic timestamps, 1 s window

        self.stats = {"sent": 0, "skipped_deadband": 0, "events_sent": 0}

        self._task: asyncio.Task | None = None

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def attach(self, bus) -> None:
        """Subscribe to the bus. Requires a running event loop (the EVENT
        subscription starts its own draining task — see model/bus.py)."""
        bus.subscribe_sync("osc-frame", (FRAME,), self._on_frame)
        bus.subscribe("osc-event", (EVENT,), self._on_event, policy=RELIABLE)
        bus.subscribe_sync("osc-meta", (META,), self._on_meta)

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.ensure_future(self.run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled

    # ── Bus handlers ─────────────────────────────────────────────────────────

    def _on_frame(self, kind, frame) -> None:
        self._latest_frame = frame

    async def _on_event(self, kind, event) -> None:
        if not self.enabled:
            return
        for route in self.routes.all():
            if not (route.enabled and route.kind == KIND_EVENT
                    and route.source == event.name):
                continue
            value = None
            if route.payload_field is not None:
                raw = event.payload.get(route.payload_field)
                if raw is None:
                    continue          # this event doesn't carry the field wanted
                value = transform(route, float(raw))
            self._send_route(route, value)
            self.stats["events_sent"] += 1

    def _on_meta(self, kind, meta) -> None:
        if getattr(meta, "topic", None) == "reset":
            self._last_value.clear()

    # ── Cadenced sender (continuous routes) ─────────────────────────────────

    async def run(self) -> None:
        while True:
            await asyncio.sleep(1.0 / self.rate_hz)
            self._cadence_step()

    def _cadence_step(self) -> None:
        """
        One wake of the cadenced sender: send every enabled continuous route
        from whatever the latest frame is. Split out from `run()` so the rate
        cap — cadence calls, not frame arrivals, decide how often a route sends
        — is testable by calling this directly; the sleep itself is real time
        and not what a test should be asserting on (see tests/test_osc.py).
        """
        if not self.enabled or self._latest_frame is None:
            return
        frame = self._latest_frame
        for route in self.routes.all():
            if not (route.enabled and route.kind == KIND_SIGNAL):
                continue
            raw = read_source(frame, route.source)
            if raw is None:
                continue                    # unavailable — never send a 0 instead
            out = transform(route, raw)
            last = self._last_value.get(route.id)
            if last is not None and abs(out - last) < route.deadband:
                self.stats["skipped_deadband"] += 1
                continue
            self._send_route(route, out)

    # ── Sending ──────────────────────────────────────────────────────────────

    def _send_route(self, route: Route, value) -> None:
        args = list(route.args)
        if value is not None:
            args.append(value)
        self.live.send(route.address, args)

        now = time.monotonic()
        self._last_value[route.id]   = value
        self._last_sent_at[route.id] = now
        self._send_times.append(now)
        cutoff = now - _RATE_WINDOW_S
        while self._send_times and self._send_times[0] < cutoff:
            self._send_times.popleft()
        self.stats["sent"] += 1

    async def test_route(self, route_id: str, duration_s: float = 1.0,
                         steps: int = 20) -> None:
        """
        Sweep a route's output from `out_min` to `out_max` and back, so
        whatever it is mapped to visibly moves in Live — the practical way to
        MIDI-learn or verify a mapping without moving the wheel. Bypasses the
        deadband and the rate cap on purpose: a test sweep should move
        smoothly regardless of the route's own live-performance settings.
        """
        route = self.routes.get(route_id)
        if route is None:
            raise KeyError(f"Unknown route {route_id!r}")
        if route.kind != KIND_SIGNAL:
            raise ValueError("only a continuous (signal) route can be swept")

        half = max(1, steps // 2)
        up = [route.out_min + (route.out_max - route.out_min) * i / half
              for i in range(half + 1)]
        sweep = up + list(reversed(up))
        delay = duration_s / max(1, len(sweep))
        for value in sweep:
            self._send_route(route, value)
            await asyncio.sleep(delay)

    # ── Observation ──────────────────────────────────────────────────────────

    def snapshot(self) -> dict:
        return {
            "enabled": self.enabled,
            "rate_hz": self.rate_hz,
            "out_hz":  round(len(self._send_times) / _RATE_WINDOW_S, 1),
            "live":    self.live.snapshot(),
            "routes":  self.routes.snapshot(),
            **self.stats,
            "last_value": dict(self._last_value),
        }
