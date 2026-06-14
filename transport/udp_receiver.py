"""
transport/udp_receiver.py — UDP receiver for BNO08x sensor data.

Receives datagrams from the ESP32, parses them according to protocol v1,
and pushes valid packets onto the central asyncio Queue.

Protocol (firmware 260524_BNO_super_reports):

  DataHeader (12 bytes, little-endian):
    uint8  version       always 1
    uint8  type          PacketType (see below)
    uint16 size          total datagram size (header + payload)
    uint32 seq           per-stream sequence number
    uint32 ts_esp_us     micros() at send time (µs)

  Packet types:
    0x01  GYRO           Vec3   gx, gy, gz         12 bytes
    0x02  ACCEL          Vec3   ax, ay, az          12 bytes
    0x03  MAG            Vec3   mx, my, mz          12 bytes
    0x04  LINEAR_ACCEL   Vec3   lax, lay, laz       12 bytes
    0x05  RV             Quat   w, x, y, z          16 bytes
    0x06  GEO_RV         Quat   w, x, y, z          16 bytes
    0x07  GAME_RV        Quat   w, x, y, z          16 bytes
    0x08  ARVR_RV        Quat   w, x, y, z          16 bytes
    0x10–0x17 SUPER_0–7  concatenated dep payloads  variable
    0x20  HEARTBEAT      ESP health beacon          24 bytes (see below)
    0x30  CFG_ACK        handled by EspConfigurator, silently ignored here

  Heartbeat payload (24 bytes, little-endian <IIIiff):
    uint32 uptime_ms       millis() since boot
    uint32 packets_sent    total UDP data packets sent since boot
    uint32 udp_errors      endPacket() failures since boot
    int32  rssi_dbm        WiFi.RSSI()
    float  cpu_temp_c      temperatureRead()
    float  battery_pct     cellPercent(), or -1 if no fuel gauge

Super-slot field naming:

  When a SuperSlotLayout is provided and the dep list for the incoming super
  slot is known, payload fields are emitted as named fields (gyro_x, gyro_y,
  game_rv_qw, …) and dep_slots is included in the packet.

  When the layout is unavailable (before the first ACK), the parser falls
  back to generic s0..sN naming and sets dep_slots=None.  A debug-level
  warning is emitted; call get_state() on EspConfigurator to populate the
  layout.
"""

import asyncio
import struct
import time
import logging

from transport.super_layout import (
    SLOT_FIELDS, SLOT_FLOAT_COUNT, SuperSlotLayout
)

log = logging.getLogger("udp_receiver")

HEADER      = struct.Struct("<BBHII")   # 12 bytes
HEADER_SIZE = HEADER.size

VEC3_TYPES = frozenset({0x01, 0x02, 0x03, 0x04})
QUAT_TYPES = frozenset({0x05, 0x06, 0x07, 0x08})
SUPER_BASE, SUPER_MAX = 0x10, 0x17
HB_TYPE   = 0x20
ACK_TYPE  = 0x30

HEARTBEAT = struct.Struct("<IIIiff")   # 24 bytes

# Expected payload size for fixed-size types (excludes super and ACK)
FIXED_PAYLOAD_SIZE = {
    0x01: 12, 0x02: 12, 0x03: 12, 0x04: 12,   # Vec3
    0x05: 16, 0x06: 16, 0x07: 16, 0x08: 16,   # Quat
    0x20: HEARTBEAT.size,                       # HEARTBEAT
}

TYPE_NAME = {
    0x01: "gyro",         0x02: "accel",    0x03: "mag",
    0x04: "linear_accel", 0x05: "rv",       0x06: "geo_rv",
    0x07: "game_rv",      0x08: "arvr_rv",  0x20: "heartbeat",
}
for _i in range(8):
    TYPE_NAME[0x10 + _i] = f"super_{_i}"


def parse_packet(
    data: bytes,
    layout: SuperSlotLayout | None = None,
) -> dict | None:
    """
    Parse a raw UDP datagram.

    Returns a packet dict on success, or None if the datagram is invalid or
    should be silently ignored (e.g. CFG_ACK handled by EspConfigurator).

    For super-slot packets (0x10–0x17), payload fields are named using the
    component sensor names when `layout` contains the dep list for that slot
    (e.g. gyro_x, game_rv_qw).  Falls back to s0..sN if the layout is
    unavailable.
    """
    if len(data) < HEADER_SIZE:
        log.warning(f"Packet too short: {len(data)} bytes")
        return None

    version, type_id, size, seq, ts_esp_us = HEADER.unpack_from(data)

    if size != len(data):
        log.warning(f"Size mismatch (header={size}, actual={len(data)})")
        return None

    if type_id == ACK_TYPE:
        return None   # handled by EspConfigurator

    if type_id not in TYPE_NAME:
        log.warning(f"Unknown type: 0x{type_id:02X}")
        return None

    payload = data[HEADER_SIZE:]

    expected = FIXED_PAYLOAD_SIZE.get(type_id)
    if expected is not None and len(payload) != expected:
        log.warning(
            f"Payload size mismatch (typeId=0x{type_id:02X}): "
            f"got {len(payload)}B, expected {expected}B"
        )
        return None

    if SUPER_BASE <= type_id <= SUPER_MAX and len(payload) < 4:
        log.warning(f"Empty super-slot payload (typeId=0x{type_id:02X})")
        return None

    ts_rx_us = time.time_ns() // 1000

    packet: dict = {
        "version":   version,
        "type":      TYPE_NAME[type_id],
        "typeId":    type_id,
        "seq":       seq,
        "ts_esp_us": ts_esp_us,
        "ts_rx_us":  ts_rx_us,
    }

    if type_id in VEC3_TYPES:
        x, y, z = struct.unpack_from("<3f", payload)
        packet.update(x=x, y=y, z=z)

    elif type_id in QUAT_TYPES:
        qw, qx, qy, qz = struct.unpack_from("<4f", payload)
        packet.update(qw=qw, qx=qx, qy=qy, qz=qz)

    elif SUPER_BASE <= type_id <= SUPER_MAX:
        super_idx = type_id - SUPER_BASE
        deps = layout.get_deps(super_idx) if layout is not None else None

        if deps is None:
            # Layout not yet known — fall back to generic s{i} naming
            n = len(payload) // 4
            floats = struct.unpack_from(f"<{n}f", payload)
            for i, v in enumerate(floats):
                packet[f"s{i}"] = v
            packet["dep_slots"] = None
            log.debug(
                f"Super slot {super_idx}: layout unknown, using s{{i}} fallback "
                "(call get_state() to populate the layout)"
            )
        else:
            expected_size = sum(SLOT_FLOAT_COUNT[si] for si in deps) * 4
            if len(payload) != expected_size:
                log.warning(
                    f"Super slot {super_idx}: payload {len(payload)}B ≠ "
                    f"{expected_size}B expected for deps={deps}"
                )
                return None
            off = 0
            for si in deps:
                n = SLOT_FLOAT_COUNT[si]
                vals = struct.unpack_from(f"<{n}f", payload, off)
                for fname, val in zip(SLOT_FIELDS[si], vals):
                    packet[fname] = val
                off += n * 4
            packet["dep_slots"] = list(deps)

    elif type_id == HB_TYPE:
        (uptime_ms, packets_sent, udp_errors,
         rssi_dbm, cpu_temp_c, battery_pct) = HEARTBEAT.unpack_from(payload)
        packet.update(
            uptime_ms=uptime_ms,
            packets_sent=packets_sent,
            udp_errors=udp_errors,
            rssi_dbm=rssi_dbm,
            cpu_temp_c=cpu_temp_c,
            battery_pct=battery_pct,
        )

    return packet


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
