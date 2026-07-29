"""
model/bus.py — In-process fan-out from the model to every output.

The model publishes Python objects.  Whoever wants them subscribes.  The
WebSocket server is *one* subscriber (it happens to serialise to JSON); the
scope ring, the event log and, later, the OSC bridge and the lighting output are
others.  None of them is privileged, and adding one is a single `subscribe`
call — that is the extension point the whole design leans on.

Why not route the OSC bridge through the WebSocket
--------------------------------------------------
It runs in this very process.  Serialising to JSON, pushing it through a TCP
socket and parsing it back would buy nothing, and it would inherit the
*drop-oldest* policy the fan-out needs for display — which is precisely wrong
for a trigger.  A subscriber that must not miss an event says so, and gets a
path that does not drop.

Loss policy is per subscription, not per subscriber
---------------------------------------------------
The same consumer usually wants both behaviours: a browser should drop stale
frames (the freshest one is the only one worth drawing) and must not drop
events.  So it subscribes twice, once per topic, with the policy that fits.

  LOSSY     bounded backlog, oldest discarded first.  For continuous values.
  RELIABLE  a backlog deep enough that overflow means the subscriber is dead
            rather than slow.  Overflow is counted and logged as an error —
            silence would be the real bug.

publish() never awaits and never blocks.  A subscriber that cannot keep up
damages only itself; the model is never held back for it.  That property is not
an optimisation, it is what keeps a paused replay from carrying on for seconds
on screen (see transport/ws_server.py for the incident that established it).
"""

import asyncio
import logging
from collections import deque
from typing import Callable, Iterable

from model.types import KINDS

log = logging.getLogger("model.bus")

LOSSY    = "lossy"
RELIABLE = "reliable"
# Not a policy you choose: what an inline subscriber reports, since it has no
# backlog of its own and therefore nothing to drop at this layer.
INLINE   = "inline"

# Backlog per async subscription. The lossy budget is small on purpose: 32
# frames is ~0.3 s at 100 Hz, enough to ride out a scheduling hiccup and short
# enough that nobody can drift a second behind the live stream. The reliable
# budget is large because reaching it does not mean "busy", it means "broken".
_LOSSY_BACKLOG    = 32
_RELIABLE_BACKLOG = 4096

# One log line per subscription per this many forced losses, so a wedged
# subscriber cannot flood the journal during a show.
_OVERFLOW_LOG_EVERY = 100


class _Subscription:
    """One (topic set, handler) pair with its own backlog and loss policy."""

    __slots__ = ("name", "kinds", "policy", "handler", "sync",
                 "_dq", "_wake", "task", "delivered", "dropped", "overflows")

    def __init__(self, name: str, kinds: frozenset[str], policy: str,
                 handler: Callable, sync: bool):
        self.name    = name
        self.kinds   = kinds
        self.policy  = policy
        self.handler = handler
        self.sync    = sync

        self._dq   = deque()
        self._wake = asyncio.Event()
        self.task: asyncio.Task | None = None

        self.delivered = 0
        self.dropped   = 0    # discarded by policy — expected for LOSSY
        self.overflows = 0    # discarded despite RELIABLE — always a fault

    @property
    def _backlog(self) -> int:
        return _LOSSY_BACKLOG if self.policy == LOSSY else _RELIABLE_BACKLOG

    def offer(self, kind: str, obj) -> None:
        """Queue one object. Never blocks; drops the oldest when saturated."""
        if len(self._dq) >= self._backlog:
            self._dq.popleft()
            if self.policy == LOSSY:
                self.dropped += 1
            else:
                # A reliable subscriber this far behind is not slow, it is
                # stuck. We still drop the oldest rather than the newest: in a
                # live show the trigger that just fired matters more than one
                # from thirty seconds ago. The id gap makes the loss provable
                # downstream, and this log makes it visible here.
                self.overflows += 1
                if self.overflows % _OVERFLOW_LOG_EVERY == 1:
                    log.error(
                        f"Subscriber {self.name!r} is not draining its reliable "
                        f"backlog — {self.overflows} object(s) lost"
                    )
        self._dq.append((kind, obj))
        self._wake.set()

    async def run(self) -> None:
        """Drain the backlog into the handler. One task per subscription."""
        try:
            while True:
                while not self._dq:
                    self._wake.clear()
                    await self._wake.wait()
                kind, obj = self._dq.popleft()
                try:
                    await self.handler(kind, obj)
                    self.delivered += 1
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    # A broken subscriber must not take the bus down with it.
                    log.warning(f"Subscriber {self.name!r} raised: {e}")
        except asyncio.CancelledError:
            raise

    def stats(self) -> dict:
        return {
            "name":      self.name,
            "kinds":     sorted(self.kinds),
            "policy":    self.policy,
            "backlog":   len(self._dq),
            "delivered": self.delivered,
            "dropped":   self.dropped,
            "overflows": self.overflows,
        }


class ModelBus:
    """
    Fan-out point between the model and every output.

    Publishing is synchronous and non-blocking; delivery to async subscribers
    happens on their own tasks.  All calls are expected from the event-loop
    thread, so no locking is needed.
    """

    def __init__(self):
        self._subs: list[_Subscription] = []
        self._sync: list[_Subscription] = []
        self.published: dict[str, int] = {k: 0 for k in KINDS}

    # ── Subscribing ──────────────────────────────────────────────────────────

    def subscribe(self, name: str, kinds: Iterable[str], handler: Callable,
                  policy: str = LOSSY) -> _Subscription:
        """
        Register an async handler (`async def handler(kind, obj)`).

        Requires a running event loop: the draining task is created here.
        `policy` is RELIABLE for anything that must not miss an object.
        """
        sub = _Subscription(name, frozenset(kinds), policy, handler, sync=False)
        sub.task = asyncio.ensure_future(sub.run())
        self._subs.append(sub)
        log.info(f"Bus subscriber {name!r} → {sorted(sub.kinds)} ({policy})")
        return sub

    def subscribe_sync(self, name: str, kinds: Iterable[str],
                       handler: Callable) -> _Subscription:
        """
        Register a plain callable `handler(kind, obj)` invoked inline by publish().

        For handlers whose work is a bounded, non-blocking append — the scope
        ring, the event log, and the WebSocket fan-out (which only serialises
        and hands to per-client outboxes).  Inline means zero latency and
        structurally zero loss at this layer, which is why those do not need a
        policy here.  A handler that could ever block must use subscribe().
        """
        sub = _Subscription(name, frozenset(kinds), INLINE, handler, sync=True)
        self._sync.append(sub)
        log.info(f"Bus subscriber {name!r} → {sorted(sub.kinds)} (inline)")
        return sub

    def unsubscribe(self, sub: _Subscription) -> None:
        if sub.task is not None and not sub.task.done():
            sub.task.cancel()
        if sub in self._subs:
            self._subs.remove(sub)
        if sub in self._sync:
            self._sync.remove(sub)

    # ── Publishing ───────────────────────────────────────────────────────────

    def publish(self, kind: str, obj) -> None:
        """Hand one object to every subscriber of `kind`. Never blocks."""
        self.published[kind] = self.published.get(kind, 0) + 1

        for sub in self._sync:
            if kind in sub.kinds:
                try:
                    sub.handler(kind, obj)
                    sub.delivered += 1
                except Exception as e:
                    log.warning(f"Inline subscriber {sub.name!r} raised: {e}")

        for sub in self._subs:
            if kind in sub.kinds:
                sub.offer(kind, obj)

    # ── Lifecycle / observation ──────────────────────────────────────────────

    async def close(self) -> None:
        for sub in list(self._subs):
            self.unsubscribe(sub)
        self._sync.clear()

    def stats(self) -> dict:
        return {
            "published":   dict(self.published),
            "subscribers": [s.stats() for s in (*self._sync, *self._subs)],
        }
