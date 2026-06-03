"""
transport/ws_server.py — Outgoing WebSocket server.

Maintains a pool of connected clients and exposes a broadcast() method.
Contains no business logic — pure fan-out.
"""

import asyncio
import json
import logging
import websockets

log = logging.getLogger("ws_server")


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
        self.clients: set = set()
        self.stats = {"tx": 0, "errors": 0}
        self._server = None

    async def start(self):
        """Start the WebSocket server and begin accepting connections."""
        self._server = await websockets.serve(self._handler, self.host, self.port)
        log.info(f"WebSocket listening on ws://{self.host}:{self.port}")

    async def _handler(self, ws) -> None:
        """Handle a single client connection for its lifetime."""
        self.clients.add(ws)
        log.info(f"Client connected: {ws.remote_address}  ({len(self.clients)} total)")
        try:
            await ws.wait_closed()
        finally:
            self.clients.discard(ws)
            log.info(f"Client disconnected  ({len(self.clients)} remaining)")

    async def broadcast(self, packet: dict) -> None:
        """Serialise packet as JSON and send it to all connected clients."""
        if not self.clients:
            return
        msg = json.dumps(packet)
        results = await asyncio.gather(
            *[c.send(msg) for c in self.clients],
            return_exceptions=True,
        )
        for r in results:
            if isinstance(r, Exception):
                self.stats["errors"] += 1
                log.debug(f"Client send error: {r}")
        self.stats["tx"] += 1
