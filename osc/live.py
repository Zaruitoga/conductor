"""
osc/live.py — The AbletonOSC conversation: send, reply socket, health, discovery.

Two independent capabilities share one socket pair here:

  * fire-and-forget sends — what the bridge does every tick, and what a route's
    test sweep does to make a mapped control visibly move in Live
  * request/reply — used only for discovery (track/device/parameter names) and
    the periodic /live/test health probe, never on the frame-sending path

AbletonOSC replies to a query on the *same address* it was asked, so a single
dict of pending futures keyed by address correlates a reply to its request.
Two concurrent requests to the *same* address would race and the first caller
would time out — acceptable here, since discovery and health probing are rare,
user- or timer-triggered actions that this module never issues concurrently
against themselves in normal operation.

The addresses this module queries (`/live/song/get/track_names`,
`/live/device/get/names`, `/live/device/get/parameters/name`, `/live/test`)
belong to the AbletonOSC project (github.com/ideoforms/AbletonOSC). Verify them
against the installed version if discovery ever comes back empty with Live
actually running — see osc/targets.py's module docstring for the same caveat
applied to the send-side addresses.
"""

import asyncio
import logging

from pythonosc import udp_client
from pythonosc.dispatcher import Dispatcher
from pythonosc.osc_server import AsyncIOOSCUDPServer

log = logging.getLogger("osc.live")

DEFAULT_REPLY_TIMEOUT_S = 1.0
HEALTH_ADDRESS   = "/live/test"
HEALTH_INTERVAL_S = 2.0


class LiveLink:
    """Owns the outgoing client and the incoming reply socket to AbletonOSC."""

    def __init__(self, host: str = "127.0.0.1", send_port: int = 11000,
                 listen_host: str = "0.0.0.0", listen_port: int = 11001):
        self.host       = host
        self.send_port  = send_port
        self.listen_host = listen_host
        self.listen_port = listen_port

        self._client = udp_client.SimpleUDPClient(host, send_port)
        self._dispatcher = Dispatcher()
        self._dispatcher.map("/live/*", self._on_reply)
        self._transport = None
        self._pending: dict[str, asyncio.Future] = {}
        self._health_task: asyncio.Task | None = None

        self.online = False
        self.stats = {"sent": 0, "errors": 0, "replies": 0}

        # Discovery cache: populated on demand (refresh_*), read instantly by
        # the panel (discovery_snapshot). Never populated eagerly — a set can
        # have dozens of tracks each with dozens of devices, and fetching every
        # parameter of every device up front would be a lot of round trips for
        # names nobody asked to see yet.
        self.tracks:     list[str] = []
        self.devices:    dict[int, list[str]] = {}          # track -> names
        self.parameters: dict[tuple[int, int], list[str]] = {}   # (track, device) -> names

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        server = AsyncIOOSCUDPServer(
            (self.listen_host, self.listen_port), self._dispatcher, loop)
        self._transport, _ = await server.create_serve_endpoint()
        self._health_task = asyncio.ensure_future(self._health_loop())
        log.info(f"OSC reply socket listening on {self.listen_host}:{self.listen_port}")

    async def stop(self) -> None:
        if self._health_task is not None:
            self._health_task.cancel()
            self._health_task = None
        if self._transport is not None:
            self._transport.close()
            self._transport = None
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()

    def retarget(self, host: str, send_port: int) -> None:
        """Point outgoing sends at a new host:port, live — no restart needed."""
        self.host, self.send_port = host, send_port
        self._client = udp_client.SimpleUDPClient(host, send_port)

    # ── Sending ──────────────────────────────────────────────────────────────

    def send(self, address: str, args: list) -> None:
        """
        Fire-and-forget. UDP: a send that fails *locally* raises (a bad socket),
        which is why this still guards with try/except — but a send Live never
        receives raises nothing at all, which is exactly what /live/test and
        `online` are for.
        """
        try:
            self._client.send_message(address, list(args))
            self.stats["sent"] += 1
        except OSError as e:
            self.stats["errors"] += 1
            log.debug(f"OSC send to {address} failed: {e}")

    # ── Request / reply ──────────────────────────────────────────────────────

    def _on_reply(self, address: str, *args) -> None:
        self.stats["replies"] += 1
        self.online = True
        fut = self._pending.get(address)
        if fut is not None and not fut.done():
            fut.set_result(args)

    async def request(self, address: str, args: list | None = None,
                      timeout: float = DEFAULT_REPLY_TIMEOUT_S) -> tuple | None:
        """Send `address` and await its reply args, or None on timeout."""
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._pending[address] = fut
        self.send(address, args or [])
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            self._pending.pop(address, None)

    # ── Health ───────────────────────────────────────────────────────────────

    async def _health_loop(self) -> None:
        while True:
            await asyncio.sleep(HEALTH_INTERVAL_S)
            reply = await self.request(HEALTH_ADDRESS, timeout=HEALTH_INTERVAL_S * 0.75)
            # The exact reply payload is not parsed — only its presence proves
            # AbletonOSC is there to answer. Absence is the whole test.
            self.online = reply is not None

    def snapshot(self) -> dict:
        return {
            "online":      self.online,
            "host":        self.host,
            "send_port":   self.send_port,
            "listen_port": self.listen_port,
            **self.stats,
        }

    # ── Discovery ────────────────────────────────────────────────────────────
    # Used only by the panel, on demand — never on the per-tick send path.

    async def track_names(self) -> list[str]:
        reply = await self.request("/live/song/get/track_names")
        return list(reply) if reply else []

    async def device_names(self, track: int) -> list[str]:
        reply = await self.request("/live/device/get/names", [track])
        # AbletonOSC echoes the request's own args before the reply payload.
        return list(reply[1:]) if reply else []

    async def parameter_names(self, track: int, device: int) -> list[str]:
        reply = await self.request("/live/device/get/parameters/name", [track, device])
        return list(reply[2:]) if reply else []

    # ── Discovery cache ──────────────────────────────────────────────────────
    # refresh_* re-queries Live and updates the cache; discovery_snapshot just
    # reads it — the split behind GET /api/osc/live (instant) and
    # POST /api/osc/live/refresh (the actual round trip) in api/routes.py.

    async def refresh_tracks(self) -> list[str]:
        self.tracks = await self.track_names()
        return self.tracks

    async def refresh_devices(self, track: int) -> list[str]:
        names = await self.device_names(track)
        self.devices[track] = names
        return names

    async def refresh_parameters(self, track: int, device: int) -> list[str]:
        names = await self.parameter_names(track, device)
        self.parameters[(track, device)] = names
        return names

    def discovery_snapshot(self) -> dict:
        return {
            "tracks":     self.tracks,
            "devices":    {str(t): v for t, v in self.devices.items()},
            "parameters": {f"{t},{d}": v for (t, d), v in self.parameters.items()},
        }
