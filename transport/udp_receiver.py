"""
transport/udp_receiver.py — UDP endpoint for BNO08x sensor data.

Receives datagrams from the ESP32, parses them via `protocol.parse_packet`,
and pushes valid packets onto the central asyncio Queue.  The wire format
itself lives in `transport/protocol.py`; this module is pure socket I/O.

Admission: an optional `accept` predicate decides whether a parsed packet is
enqueued.  The receiver holds no policy of its own — core supplies the callable
(it is what mutes live input while a replay owns the pipeline).

Host-side loss (#76): the receiver is the one place on the host where a packet
can be thrown away before `processing_loop` sees it, and a packet lost here
digs the same `seq` hole as one lost in the air.  So both paths are dealt with:
a full queue is counted (`stats["dropped"]`) rather than merely logged, and the
kernel's receive buffer is sized explicitly (`config.UDP_RCVBUF`) rather than
left at the OS default.  The kernel path itself stays unobserved — a socket
buffer overflow never reaches `datagram_received`.
"""

import asyncio
import logging
import socket
import time
from typing import Callable

import config
from transport.super_layout import SuperSlotLayout
from transport.protocol import parse_packet

log = logging.getLogger("udp_receiver")

# At most one "queue full" warning per this many seconds of wall clock.  A
# warning per packet floods the output during exactly the episode where one is
# already struggling; the cumulative count is in each line and in status.udp.
DROP_LOG_INTERVAL_S = 1.0


def set_rcvbuf(sock, requested: int) -> int | None:
    """
    Ask for a `requested`-byte receive buffer and return what the kernel kept.

    The kernel decides: Linux clamps to `rmem_max` silently (and reports double),
    macOS *refuses* anything above ~maxsockbuf·2048/2304 with ENOBUFS.  So a
    refusal halves the request and tries again, down to whatever was already in
    place — never raising, since a socket that works with the default buffer is
    worth more than an orchestrator that will not start.  Returns the effective
    value read back with getsockopt, or None if even that read failed.
    """
    try:
        current = sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
    except OSError as exc:
        log.warning(f"SO_RCVBUF unreadable ({exc}) — keeping the default buffer")
        return None

    size = requested
    while size > current:
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, size)
            break
        except OSError as exc:
            log.warning(f"SO_RCVBUF={size} refused ({exc})")
            size //= 2
    try:
        return sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
    except OSError:
        return current


class UDPReceiver(asyncio.DatagramProtocol):
    """
    asyncio UDP protocol.

    Each valid packet is placed on `queue` for downstream processing, unless the
    `accept` predicate rejects it (counted in stats["muted"]) or the queue is
    full (counted in stats["dropped"], warned about at most once a second).
    Tracks the last source IP so the configurator can auto-detect the ESP address.
    A shared SuperSlotLayout is used to decode super-slot payloads into named fields.
    """

    def __init__(
        self,
        queue: asyncio.Queue,
        layout: SuperSlotLayout | None = None,
        accept: Callable[[dict], bool] | None = None,
    ):
        self.queue         = queue
        self.layout        = layout
        self.accept        = accept
        self.stats         = {"rx": 0, "errors": 0, "muted": 0, "dropped": 0}
        self.last_esp_ip: str | None = None
        # Effective SO_RCVBUF as read back from the kernel; set by
        # start_udp_receiver, None until then (or if it could not be read).
        self.rcvbuf: int | None = None
        self._drop_logged_at = float("-inf")

    def connection_made(self, transport):
        log.info("UDP receiver ready")

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        self.stats["rx"] += 1
        self.last_esp_ip = addr[0]

        packet = parse_packet(data, self.layout)
        if packet is None:
            self.stats["errors"] += 1
            return

        # last_esp_ip is set above, before this gate: muting live data must not
        # stop log_stats from self-healing the ESP config target.
        if self.accept is not None and not self.accept(packet):
            self.stats["muted"] += 1
            return

        try:
            self.queue.put_nowait(packet)
        except asyncio.QueueFull:
            self.stats["dropped"] += 1
            now = time.monotonic()
            if now - self._drop_logged_at >= DROP_LOG_INTERVAL_S:
                self._drop_logged_at = now
                log.warning(f"Queue full — packets dropped "
                            f"({self.stats['dropped']} since start)")

    def error_received(self, exc: Exception) -> None:
        log.error(f"UDP error: {exc}")

    def connection_lost(self, exc):
        log.warning("UDP connection lost")


async def start_udp_receiver(
    host: str,
    port: int,
    queue: asyncio.Queue,
    layout: SuperSlotLayout | None = None,
    accept: Callable[[dict], bool] | None = None,
):
    """
    Create and bind the UDP endpoint. Returns (transport, protocol).

    Pass the shared SuperSlotLayout so that super-slot packets are decoded
    into named fields as soon as the layout is populated by EspConfigurator.
    `accept` is an optional admission predicate applied to each parsed packet.
    """
    loop = asyncio.get_running_loop()
    transport, protocol = await loop.create_datagram_endpoint(
        lambda: UDPReceiver(queue, layout, accept),
        local_addr=(host, port),
    )
    sock = transport.get_extra_info("socket")
    if sock is not None:
        protocol.rcvbuf = set_rcvbuf(sock, config.UDP_RCVBUF)
        log.info(f"UDP SO_RCVBUF: {protocol.rcvbuf} bytes "
                 f"(asked {config.UDP_RCVBUF})")
    log.info(f"UDP listening on {host}:{port}")
    return transport, protocol
