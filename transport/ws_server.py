"""
transport/ws_server.py — Outgoing WebSocket server.

One of the model bus's subscribers (see model/bus.py), and nothing more: it
serialises what it is handed and fans it out to connected browsers.  No business
logic, no knowledge of what a signal means.

Fan-out policy: **a slow client never slows the pipeline down.**  Each client
owns its own outbox drained by its own writer task, so publishing only ever does
a non-blocking append.

Before this, broadcast() awaited send() for every client inside the processing
loop: one slow browser was enough to stall the whole orchestrator (measured: the
central queue grew past 6000 packets and the pipeline fell from ~380 to 186
packets/s with a single visualiser attached).

Two classes of message, two behaviours
--------------------------------------
Continuous output (raw sensor packets, model frames) behaves like a controller:
for a real-time view the freshest one is the only one worth drawing, and a
backlog is worse than a gap.  Those are **droppable**.

Events behave like triggers: missing one is a fault, not a blur.  Those are
**not droppable**, they live in a separate outbox with far more headroom, and
the writer drains them first — during a show, a trigger that just fired matters
more than the frame that was going to redraw the wheel.  A saturated client
therefore loses smoothness and keeps its triggers, which is the right trade.

Subscription: a client may narrow what it receives with a query string, e.g.
`ws://host:8081/?types=frame` or `?types=event,meta`.  No query string means
"send me everything", so existing consumers are unaffected.  The names match the
`type` field of what is sent: a wire packet's own name (`gyro`, `super_0`,
`heartbeat`…) or one of `frame` / `event` / `meta`.
"""

import asyncio
import json
import logging
from collections import deque
from urllib.parse import parse_qs, urlparse

import websockets

from model.types import RAW, FRAME, EVENT, META

log = logging.getLogger("ws_server")

# Droppable backlog per client. 8 messages is ~80 ms at 100 Hz: enough to ride
# out a scheduling hiccup, short enough that a client can never drift seconds
# behind the live stream.
_LOSSY_BACKLOG = 8

# Non-droppable backlog per client. Events are sparse, so this is minutes' worth
# of headroom: reaching it means the socket is wedged, not busy.
_RELIABLE_BACKLOG = 512

# Kinds whose loss is acceptable — see the module docstring.
_DROPPABLE = frozenset({RAW, FRAME})


class _Outbox:
    """
    One client's pending messages, split by whether they may be discarded.

    Two deques rather than one queue with a priority scan: the split is O(1) in
    both directions and makes the policy readable — a droppable message can
    never evict a trigger, and a trigger can never be evicted by a flood of
    frames.
    """

    __slots__ = ("_lossy", "_reliable", "_wake", "dropped", "forced")

    def __init__(self):
        self._lossy    = deque()
        self._reliable = deque()
        self._wake     = asyncio.Event()
        self.dropped   = 0   # discarded by policy — expected under load
        self.forced    = 0   # a trigger discarded anyway — always a fault

    def put(self, msg: str, droppable: bool) -> None:
        if droppable:
            if len(self._lossy) >= _LOSSY_BACKLOG:
                self._lossy.popleft()
                self.dropped += 1
            self._lossy.append(msg)
        else:
            if len(self._reliable) >= _RELIABLE_BACKLOG:
                self._reliable.popleft()
                self.forced += 1
                if self.forced % 100 == 1:
                    log.error(
                        f"Client outbox saturated with undroppable messages — "
                        f"{self.forced} event(s) lost to a wedged socket"
                    )
            self._reliable.append(msg)
        self._wake.set()

    async def get(self) -> str:
        """Next message to send. Triggers jump the queue ahead of frames."""
        while not self._reliable and not self._lossy:
            self._wake.clear()
            await self._wake.wait()
        if self._reliable:
            return self._reliable.popleft()
        return self._lossy.popleft()

    @property
    def depth(self) -> int:
        return len(self._lossy) + len(self._reliable)


class _Client:
    """One connected client: its socket, its outbox and its type filter."""

    __slots__ = ("ws", "outbox", "task", "types")

    def __init__(self, ws, types: frozenset[str] | None):
        self.ws     = ws
        self.types  = types          # None = every type
        self.outbox = _Outbox()
        self.task: asyncio.Task | None = None

    def wants(self, wire_type) -> bool:
        return self.types is None or wire_type in self.types


class WSServer:
    """
    Manages the WebSocket client pool and fans out what the bus publishes.

    Usage:
        server = WSServer(host, port)
        await server.start()
        server.attach(bus)          # becomes a bus subscriber
    """

    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self.clients: dict = {}      # ws -> _Client  (len() gives the count)
        self.stats = {"tx": 0, "errors": 0, "dropped": 0, "forced": 0}
        self._server = None
        self._sub = None

    async def start(self):
        """Start the WebSocket server and begin accepting connections."""
        self._server = await websockets.serve(self._handler, self.host, self.port)
        log.info(f"WebSocket listening on ws://{self.host}:{self.port}")

    def attach(self, bus) -> None:
        """
        Subscribe to the bus.

        Inline (subscribe_sync) rather than with its own backlog: the handler
        only serialises and appends to per-client outboxes, which is bounded and
        never blocks.  A second layer of queueing here would buffer nothing and
        would only add latency between a trigger firing and it reaching the wire.
        """
        self._sub = bus.subscribe_sync("ws", (RAW, FRAME, EVENT, META), self.publish)

    # ── Connection handling ──────────────────────────────────────────────────

    @staticmethod
    def _requested_types(ws) -> frozenset[str] | None:
        """
        Parse `?types=a,b` from the connection URL, or None for "everything".

        The request path lives at `ws.path` on the legacy implementation and at
        `ws.request.path` on the new asyncio one; support both.
        """
        path = getattr(ws, "path", None)
        if path is None:
            path = getattr(getattr(ws, "request", None), "path", "")
        raw = parse_qs(urlparse(path or "").query).get("types", [])
        types = {t for item in raw for t in item.split(",") if t}
        return frozenset(types) or None

    async def _handler(self, ws) -> None:
        """Handle a single client connection for its lifetime."""
        client = _Client(ws, self._requested_types(ws))
        self.clients[ws] = client
        client.task = asyncio.ensure_future(self._writer(client))
        log.info(
            f"Client connected: {ws.remote_address}  ({len(self.clients)} total"
            f"{'' if client.types is None else ', types=' + ','.join(sorted(client.types))})"
        )
        try:
            await ws.wait_closed()
        finally:
            client.task.cancel()
            self.stats["dropped"] += client.outbox.dropped
            self.stats["forced"]  += client.outbox.forced
            self.clients.pop(ws, None)
            log.info(f"Client disconnected  ({len(self.clients)} remaining)")

    async def _writer(self, client: _Client) -> None:
        """Drain one client's outbox. The only place a send is awaited."""
        try:
            while True:
                msg = await client.outbox.get()
                await client.ws.send(msg)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.stats["errors"] += 1
            log.debug(f"Client send error: {e}")

    # ── Fan-out ──────────────────────────────────────────────────────────────

    def publish(self, kind: str, obj) -> None:
        """
        Bus handler: serialise once and hand the result to every subscriber.

        Serialisation is lazy — a message no connected client asked for is never
        turned into JSON.  Synchronous by design: there is nothing here to await,
        and making the caller await it would only add a scheduling round trip
        between the model and the wire.
        """
        if not self.clients:
            return

        wire_type = obj.get("type") if kind == RAW else kind
        droppable = kind in _DROPPABLE
        msg: str | None = None

        for client in list(self.clients.values()):
            if not client.wants(wire_type):
                continue
            if msg is None:
                msg = json.dumps(obj if kind == RAW else obj.to_wire())
            client.outbox.put(msg, droppable)

        if msg is not None:
            self.stats["tx"] += 1

    def snapshot(self) -> dict:
        """Live counters, including backlog held by currently-connected clients."""
        live_dropped = sum(c.outbox.dropped for c in self.clients.values())
        live_forced  = sum(c.outbox.forced for c in self.clients.values())
        return {
            "clients": len(self.clients),
            "tx":      self.stats["tx"],
            "errors":  self.stats["errors"],
            "dropped": self.stats["dropped"] + live_dropped,
            "forced":  self.stats["forced"] + live_forced,
            "backlog": sum(c.outbox.depth for c in self.clients.values()),
        }
