"""
transport/protocol.py — Binary wire protocol shared with the ESP32 firmware.

Python mirror of the firmware's protocol.h.  Single source of truth for the
UDP wire format: packet/command type IDs, struct layouts, slot↔sensor naming,
and the pure parse/build functions.  Contains NO I/O and NO mutable state —
the transport modules (udp_receiver, esp_configurator) own the sockets and
call into here; the observation/health modules import the naming tables.

Two UDP channels:
  Data port   (4210)  ESP → PC   sensor packets          → parse_packet()
  Config port (4211)  PC  → ESP  commands + ACK reply     → build_*() / parse_ack()

All multi-byte fields are little-endian.

DataHeader (12 bytes, <BBHII):
    uint8  version       always 1
    uint8  type          PacketType
    uint16 size          total datagram size (header + payload)
    uint32 seq           per-stream sequence number
    uint32 ts_esp_us     micros() at send time

Packet types (ESP → PC, data port):
    0x01 GYRO  0x02 ACCEL  0x03 MAG  0x04 LINEAR_ACCEL   Vec3   12 bytes
    0x05 RV    0x06 GEO_RV 0x07 GAME_RV 0x08 ARVR_RV     Quat   16 bytes
    0x10–0x17 SUPER_0–7    concatenated dep payloads             variable
    0x20 HEARTBEAT         ESP health beacon                     24 bytes
    0x30 CFG_ACK           full-state dump                       parse_ack()

Heartbeat payload (24 bytes, <IIIiff): uptime_ms, packets_sent, udp_errors,
rssi_dbm, cpu_temp_c, battery_pct (battery -1 ⇒ no fuel gauge).

ACK body (ESP → PC, type 0x30):
    DataHeader + n_simple(B) + AckSimpleEntry[n] (12B) +
                 n_super(B)  + AckSuperEntry[n]  (14B) + host_ip[4]

Super-slot field naming: when the SuperSlotLayout knows a slot's dep list,
payload floats are emitted as named fields (gyro_x, game_rv_qw, …) and
dep_slots is included; otherwise parse_packet falls back to generic s0..sN.
"""

import struct
import time
import logging

from transport.super_layout import SuperSlotLayout

log = logging.getLogger("protocol")

# ── Headers ──────────────────────────────────────────────────────────────────
DATA_HEADER      = struct.Struct("<BBHII")   # 12 bytes: version type size seq ts_us
DATA_HEADER_SIZE = DATA_HEADER.size
CFG_HEADER       = struct.Struct("<BBH")     # 4 bytes:  version type size

# ── Packet types: ESP → PC (data port) ───────────────────────────────────────
VEC3_TYPES = frozenset({0x01, 0x02, 0x03, 0x04})
QUAT_TYPES = frozenset({0x05, 0x06, 0x07, 0x08})
SUPER_BASE, SUPER_MAX = 0x10, 0x17
HB_TYPE  = 0x20
ACK_TYPE = 0x30

HEARTBEAT = struct.Struct("<IIIiff")         # 24 bytes (see module docstring)

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

# ── Command types: PC → ESP (config port) ────────────────────────────────────
CFG_SET_SIMPLE = 0x01
CFG_SET_SUPER  = 0x02
CFG_DEL_SUPER  = 0x03
CFG_GET_STATE  = 0x04
CFG_SET_HOST   = 0x05

# ── ACK body entry structs (ESP → PC, type 0x30) ─────────────────────────────
ACK_SIMPLE = struct.Struct("<BBBBB3xI")      # 12 bytes: slot sensor pkt psz enabled _pad rate
ACK_SUPER  = struct.Struct("<BBBBBB8s")      # 14 bytes: slot pkt active ndeps skip psz deps[8]

# ── Slot ↔ sensor naming ─────────────────────────────────────────────────────
# Uppercase display names, indexed by simple slot (0–7); used in logs and ACK
# summaries.  Mirrors the firmware's SLOT_DEF order (report_manager.cpp).
SLOT_NAME = [
    "GYRO", "ACCEL", "MAG", "LINEAR_ACCEL",
    "RV", "GEO_RV", "GAME_RV", "ARVR_RV",
]

# Payload field names for each simple slot, used to decode super-slot packets
# into named fields and as the fixed CSV column set for super rows.
SLOT_FIELDS: dict[int, tuple[str, ...]] = {
    0: ("gyro_x",           "gyro_y",           "gyro_z"),
    1: ("accel_x",          "accel_y",          "accel_z"),
    2: ("mag_x",            "mag_y",            "mag_z"),
    3: ("linear_accel_x",   "linear_accel_y",   "linear_accel_z"),
    4: ("rv_qw",            "rv_qx",            "rv_qy",            "rv_qz"),
    5: ("geo_rv_qw",        "geo_rv_qx",        "geo_rv_qy",        "geo_rv_qz"),
    6: ("game_rv_qw",       "game_rv_qx",       "game_rv_qy",       "game_rv_qz"),
    7: ("arvr_rv_qw",       "arvr_rv_qx",       "arvr_rv_qy",       "arvr_rv_qz"),
}

# Number of floats in each simple slot's payload (Vec3 = 3, Quat = 4)
SLOT_FLOAT_COUNT: dict[int, int] = {
    0: 3, 1: 3, 2: 3, 3: 3,
    4: 4, 5: 4, 6: 4, 7: 4,
}

# All possible named super-payload fields, enumerated in slot order.
ALL_SUPER_NAMED_FIELDS: tuple[str, ...] = sum(
    (SLOT_FIELDS[i] for i in range(8)), ()
)


# ── Data-plane parsing (ESP → PC) ────────────────────────────────────────────

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
    if len(data) < DATA_HEADER_SIZE:
        log.warning(f"Packet too short: {len(data)} bytes")
        return None

    version, type_id, size, seq, ts_esp_us = DATA_HEADER.unpack_from(data)

    if size != len(data):
        log.warning(f"Size mismatch (header={size}, actual={len(data)})")
        return None

    if type_id == ACK_TYPE:
        return None   # handled by EspConfigurator

    if type_id not in TYPE_NAME:
        log.warning(f"Unknown type: 0x{type_id:02X}")
        return None

    payload = data[DATA_HEADER_SIZE:]

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


# ── Config-plane build/parse (PC ↔ ESP) ──────────────────────────────────────

def _datagram(cfg_type: int, body: bytes = b"") -> bytes:
    """Wrap a command body in a CfgHeader."""
    return CFG_HEADER.pack(1, cfg_type, CFG_HEADER.size + len(body)) + body


def build_set_host(ip: str) -> bytes:
    """CFG_SET_HOST: tell the ESP which IPv4 to send sensor data to."""
    body = bytes(int(b) for b in ip.split("."))
    return _datagram(CFG_SET_HOST, body)


def build_set_simple(slot: int, enabled: bool, rate_us: int) -> bytes:
    """CFG_SET_SIMPLE: enable/disable a simple slot and set its sample rate."""
    return _datagram(CFG_SET_SIMPLE, struct.pack("<BBI", slot, int(enabled), rate_us))


def build_set_super(slot: int, dep_slots: list[int], skip_ratio: int = 1) -> bytes:
    """CFG_SET_SUPER: create or replace a super slot."""
    body = struct.pack("<BBB", slot, len(dep_slots), skip_ratio) + bytes(dep_slots)
    return _datagram(CFG_SET_SUPER, body)


def build_del_super(slot: int) -> bytes:
    """CFG_DEL_SUPER: delete a super slot."""
    return _datagram(CFG_DEL_SUPER, struct.pack("<B", slot))


def build_get_state() -> bytes:
    """CFG_GET_STATE: request the full ESP state (ACK in response)."""
    return _datagram(CFG_GET_STATE)


def parse_ack(data: bytes) -> dict:
    """
    Deserialise a CFG_ACK datagram into a state dict {simples, supers, host}.

    Pure: no side effects.  The caller (EspConfigurator) is responsible for
    updating the shared layout, caching the state, and logging.
    """
    off = DATA_HEADER.size

    n_simple = data[off]; off += 1
    simples = []
    for _ in range(n_simple):
        slot, sensor, pkt, psz, enabled, rate = ACK_SIMPLE.unpack_from(data, off)
        simples.append(dict(
            slot=slot,
            sensor_id=hex(sensor),
            pkt_type=hex(pkt),
            payload_sz=psz,
            enabled=bool(enabled),
            rate_hz=round(1e6 / rate, 1) if rate else 0,
            rate_us=rate,
        ))
        off += ACK_SIMPLE.size

    n_super = data[off]; off += 1
    supers = []
    for _ in range(n_super):
        slot, pkt, active, n_deps, skip, psz, dep_raw = ACK_SUPER.unpack_from(data, off)
        supers.append(dict(
            slot=slot,
            active=bool(active),
            n_deps=n_deps,
            skip_ratio=skip,
            payload_sz=psz,
            deps=list(dep_raw[:n_deps]),
        ))
        off += ACK_SUPER.size

    host = (
        ".".join(str(b) for b in data[off:off + 4])
        if off + 4 <= len(data) else "?"
    )

    return dict(simples=simples, supers=supers, host=host)
