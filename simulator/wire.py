"""
simulator/wire.py — ESP→PC datagram emitters (the firmware side of the wire).

Mirror image of `transport/protocol.py`: everything here *builds* the datagrams
that `protocol.parse_packet` / `protocol.parse_ack` consume.

The struct layouts are **imported** from protocol.py, never redeclared, so the
two directions cannot drift apart — protocol.py stays the single source of
truth for the wire format, and this module only composes it in reverse.

Note on what this does and does not prove: because both sides share the same
Struct objects, a round trip through here cannot detect a byte-layout error —
the anchor for that remains the firmware's protocol.h. What it does exercise is
everything built on top: field naming, super-slot dep ordering, layout
propagation through the ACK, packet rates, and timestamp handling.
"""

import struct

from transport.protocol import (
    ACK_SIMPLE,
    ACK_SUPER,
    ACK_TYPE,
    DATA_HEADER,
    HB_TYPE,
    HEARTBEAT,
    SUPER_BASE,
)

PROTOCOL_VERSION = 1


def build_data(type_id: int, seq: int, ts_esp_us: int, payload: bytes) -> bytes:
    """Wrap a payload in a DataHeader. `size` is the *total* datagram length."""
    size = DATA_HEADER.size + len(payload)
    return DATA_HEADER.pack(PROTOCOL_VERSION, type_id, size, seq, ts_esp_us) + payload


def build_vec3(type_id: int, seq: int, ts_esp_us: int, xyz) -> bytes:
    """A 12-byte Vec3 packet (types 0x01–0x04)."""
    return build_data(type_id, seq, ts_esp_us, struct.pack("<3f", *xyz))


def build_quat(type_id: int, seq: int, ts_esp_us: int, wxyz) -> bytes:
    """A 16-byte Quat packet (types 0x05–0x08), field order qw qx qy qz."""
    return build_data(type_id, seq, ts_esp_us, struct.pack("<4f", *wxyz))


def build_super(slot: int, seq: int, ts_esp_us: int, values) -> bytes:
    """
    A super-slot packet (types 0x10–0x17).

    `values` is the flat float sequence obtained by concatenating each dep
    slot's payload **in dep order** — that order is what the receiver replays
    through SLOT_FIELDS to name the fields, so it must match the ACK's deps.
    """
    payload = struct.pack(f"<{len(values)}f", *values)
    return build_data(SUPER_BASE + slot, seq, ts_esp_us, payload)


def build_heartbeat(
    seq:          int,
    ts_esp_us:    int,
    uptime_ms:    int,
    packets_sent: int,
    udp_errors:   int,
    rssi_dbm:     int,
    cpu_temp_c:   float,
    battery_pct:  float,
) -> bytes:
    """The 0x20 health beacon (24-byte <IIIiff payload)."""
    payload = HEARTBEAT.pack(
        uptime_ms, packets_sent, udp_errors, rssi_dbm, cpu_temp_c, battery_pct
    )
    return build_data(HB_TYPE, seq, ts_esp_us, payload)


def build_ack(
    simples:   list[tuple],
    supers:    list[tuple],
    host_ip:   str,
    seq:       int,
    ts_esp_us: int,
) -> bytes:
    """
    Build a CFG_ACK (0x30) full-state dump — the exact counterpart of
    `protocol.parse_ack`.

    Layout: DataHeader + n_simple(B) + AckSimpleEntry[n] (12 B)
                       + n_super(B)  + AckSuperEntry[n]  (14 B) + host_ip[4]

    Entries are plain tuples so this module stays decoupled from the
    simulator's state objects:
        simples: (slot, sensor_id, pkt_type, payload_sz, enabled, rate_us)
        supers:  (slot, pkt_type, active, deps, skip_ratio, payload_sz)
    """
    body = bytes([len(simples)])
    for slot, sensor_id, pkt_type, payload_sz, enabled, rate_us in simples:
        body += ACK_SIMPLE.pack(
            slot, sensor_id, pkt_type, payload_sz, int(enabled), rate_us
        )

    body += bytes([len(supers)])
    for slot, pkt_type, active, deps, skip_ratio, payload_sz in supers:
        # deps rides in a fixed 8-byte field; the reader truncates it to n_deps
        body += ACK_SUPER.pack(
            slot, pkt_type, int(active), len(deps), skip_ratio, payload_sz,
            bytes(deps[:8]).ljust(8, b"\x00"),
        )

    body += bytes(int(b) for b in host_ip.split("."))

    size = DATA_HEADER.size + len(body)
    return DATA_HEADER.pack(PROTOCOL_VERSION, ACK_TYPE, size, seq, ts_esp_us) + body
