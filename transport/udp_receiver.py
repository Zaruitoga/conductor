"""
transport/udp_receiver.py — UDP endpoint for BNO08x sensor data.

Receives datagrams from the ESP32, parses them via `protocol.parse_packet`,
and pushes valid packets onto the central asyncio Queue.  The wire format
itself lives in `transport/protocol.py`; this module is pure socket I/O.
"""

import asyncio
import logging

from transport.super_layout import SuperSlotLayout
from transport.protocol import parse_packet

log = logging.getLogger("udp_receiver")


class UDPReceiver(asyncio.DatagramProtocol):
    """
    asyncio UDP protocol.

    Each valid packet is placed on `queue` for downstream processing.
    Tracks the last source IP so the configurator can auto-detect the ESP address.
    A shared SuperSlotLayout is used to decode super-slot payloads into named fields.
    """

    def __init__(
        self,
        queue: asyncio.Queue,
        layout: SuperSlotLayout | None = None,
    ):
        self.queue         = queue
        self.layout        = layout
        self.stats         = {"rx": 0, "errors": 0}
        self.last_esp_ip: str | None = None

    def connection_made(self, transport):
        log.info("UDP receiver ready")

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        self.stats["rx"] += 1
        self.last_esp_ip = addr[0]

        packet = parse_packet(data, self.layout)
        if packet is None:
            self.stats["errors"] += 1
            return

        try:
            self.queue.put_nowait(packet)
        except asyncio.QueueFull:
            log.warning("Queue full — packet dropped")

    def error_received(self, exc: Exception) -> None:
        log.error(f"UDP error: {exc}")

    def connection_lost(self, exc):
        log.warning("UDP connection lost")


async def start_udp_receiver(
    host: str,
    port: int,
    queue: asyncio.Queue,
    layout: SuperSlotLayout | None = None,
):
    """
    Create and bind the UDP endpoint. Returns (transport, protocol).

    Pass the shared SuperSlotLayout so that super-slot packets are decoded
    into named fields as soon as the layout is populated by EspConfigurator.
    """
    loop = asyncio.get_running_loop()
    transport, protocol = await loop.create_datagram_endpoint(
        lambda: UDPReceiver(queue, layout),
        local_addr=(host, port),
    )
    log.info(f"UDP listening on {host}:{port}")
    return transport, protocol
