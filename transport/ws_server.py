"""
transport/ws_server.py — Outgoing WebSocket server.

Maintains a pool of connected clients and exposes a broadcast() method.
Contains no business logic — pure fan-out.

Fan-out policy: **a slow client never slows the pipeline down.**  Each client
owns a small bounded queue drained by its own writer task, so broadcast() only
ever does a non-blocking put.  When a client cannot keep up, its oldest pending
packet is dropped (counted in stats["dropped"]) — for a real-time view the
freshest packet is the one that matters, and a backlog is worse than a gap.

Before this, broadcast() awaited send() for every client inside
processing_loop: one slow browser was enough to stall the whole orchestrator
(measured: the central queue grew past 6000 packets and the pipeline fell from
~380 to 186 packets/s with a single visualiser attached).

Subscription: a client may narrow what it receives with a query string, e.g.
`ws://host:8081/?types=computed`.  No query string means "send me everything",
so existing consumers are unaffected.
"""

import asyncio
import json
import logging
from urllib.parse import parse_qs, urlparse

import websockets

log = logging.getLogger("ws_server")

# Pending packets kept per client before the oldest is dropped. 8 packets is
# ~80 ms at 100 Hz: enough to ride out a scheduling hiccup, short enough that a
# client can never drift seconds behind the live stream.
_CLIENT_QUEUE_SIZE = 8


class _Client:
    """One connected client: its socket, its outbox and its type filter."""

    __slots__ = ("ws", "queue", "task", "types")

    def __init__(self, ws, types: frozenset[str] | None):
        self.ws    = ws
        self.types = types          # None = every packet type
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=_CLIENT_QUEUE_SIZE)
        self.task: asyncio.Task | None = None

    def wants(self, packet_type) -> bool:
        return self.types is None or packet_type in self.types


class WSServer:
    """
    Manages the WebSocket client pool and broadcasts enriched packets.

    Usage:
        server = WSServer(host, port)
        await server.start()
        await server.broadcast(packet_dict)
    """

    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self.clients: dict = {}      # ws -> _Client  (len() gives the count)
        self.stats = {"tx": 0, "errors": 0, "dropped": 0}
        self._server = None

    async def start(self):
        """Start the WebSocket server and begin accepting connections."""
        self._server = await websockets.serve(self._handler, self.host, self.port)
        log.info(f"WebSocket listening on ws://{self.host}:{self.port}")

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
            self.clients.pop(ws, None)
            log.info(f"Client disconnected  ({len(self.clients)} remaining)")

    async def _writer(self, client: _Client) -> None:
        """Drain one client's outbox. The only place a send is awaited."""
        try:
            while True:
                msg = await client.queue.get()
                await client.ws.send(msg)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.stats["errors"] += 1
            log.debug(f"Client send error: {e}")

    def _enqueue(self, client: _Client, msg: str) -> None:
        """Queue a message, dropping the oldest one rather than ever blocking."""
        try:
            client.queue.put_nowait(msg)
            return
        except asyncio.QueueFull:
            pass
        try:
            client.queue.get_nowait()      # make room: the stalest packet goes
        except asyncio.QueueEmpty:
            pass
        self.stats["dropped"] += 1
        try:
            client.queue.put_nowait(msg)
        except asyncio.QueueFull:
            pass                            # writer raced us; the packet is lost

    async def broadcast(self, packet: dict) -> None:
        """
        Hand the packet to every subscribed client's outbox. Never blocks.

        Serialisation is lazy: a packet no connected client subscribes to is
        never turned into JSON.
        """
        if not self.clients:
            return

        packet_type = packet.get("type")
        msg: str | None = None

        for client in list(self.clients.values()):
            if not client.wants(packet_type):
                continue
            if msg is None:
                msg = json.dumps(packet)
            self._enqueue(client, msg)

        if msg is not None:
            self.stats["tx"] += 1
