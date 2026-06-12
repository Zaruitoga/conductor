"""
transport/esp_configurator.py — UDP config client for the ESP32.

Sends configuration commands to the ESP32 on the config port (4211) and
parses the full-state ACK returned after each command.

After each ACK, the shared SuperSlotLayout is updated so that the UDP
receiver can immediately decode subsequent super-slot packets into properly
named fields.

Config protocol (PC → ESP):

  CfgHeader (4 bytes): version(B) type(B) size(H)
  Body by type:
    CFG_SET_SIMPLE (0x01)  slot(B) enabled(B) rate_us(I)
    CFG_SET_SUPER  (0x02)  slot(B) n_deps(B) skip(B) dep_slots[n_deps]
    CFG_DEL_SUPER  (0x03)  slot(B)
    CFG_GET_STATE  (0x04)  (no body)
    CFG_SET_HOST   (0x05)  ip[4]

ACK format (ESP → PC):

  DataHeader (12 bytes) + n_simple(B) + AckSimpleEntry[8] (12 bytes each)
                        + n_super(B)  + AckSuperEntry[8]  (14 bytes each)
                        + host_ip[4]

All public methods are blocking (< 2 s timeout).
Call from asyncio via: await loop.run_in_executor(None, cfg.method, ...)
"""

import socket
import struct
import logging

from transport.super_layout import SuperSlotLayout

log = logging.getLogger("esp_configurator")

CFG_SET_SIMPLE = 0x01
CFG_SET_SUPER  = 0x02
CFG_DEL_SUPER  = 0x03
CFG_GET_STATE  = 0x04
CFG_SET_HOST   = 0x05

CFG_HDR    = struct.Struct("<BBH")       # 4 bytes  : version type size
DATA_HDR   = struct.Struct("<BBHII")    # 12 bytes : version type size seq ts_us
ACK_SIMPLE = struct.Struct("<BBBBB3xI") # 12 bytes : slot sensor pkt psz enabled _pad rate
ACK_SUPER  = struct.Struct("<BBBBBB8s") # 14 bytes : slot pkt active ndeps skip psz deps[8]

SLOT_NAME = [
    "GYRO", "ACCEL", "MAG", "LINEAR_ACCEL",
    "RV", "GEO_RV", "GAME_RV", "ARVR_RV",
]


class EspConfigurator:
    """
    UDP configuration client for the ESP32.

    Accepts an optional SuperSlotLayout reference; the layout is updated
    automatically after every ACK so that the UDP receiver can decode
    super-slot packets into named fields without delay.

    Typical usage:
        layout = SuperSlotLayout()
        cfg = EspConfigurator("10.89.55.66", 4211, 4211, layout=layout)
        cfg.start()
        state = cfg.get_state()        # also populates layout
        cfg.set_simple(slot=0, enabled=True, rate_us=20_000)
        cfg.stop()
    """

    def __init__(
        self,
        esp_ip:      str,
        config_port: int,
        local_port:  int,
        timeout:     float = 2.0,
        layout:      SuperSlotLayout | None = None,
    ):
        self._esp_ip      = esp_ip
        self._config_port = config_port
        self._local_port  = local_port
        self._timeout     = timeout
        self._layout      = layout
        self._sock: socket.socket | None = None

        # Last ACK-parsed state {simples, supers, host}, or None until the first
        # ACK arrives. The ESP config only changes via our own commands (each
        # returns an ACK), so this cache is authoritative — no polling needed.
        self.state: dict | None = None

    def start(self) -> None:
        """Open and bind the UDP socket."""
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind(("", self._local_port))
        self._sock.settimeout(self._timeout)
        log.info(
            f"Configurator ready → {self._esp_ip}:{self._config_port} "
            f"(local port {self._local_port})"
        )

    def stop(self) -> None:
        """Close the UDP socket."""
        if self._sock:
            self._sock.close()
            self._sock = None

    @property
    def esp_ip(self) -> str:
        return self._esp_ip

    @esp_ip.setter
    def esp_ip(self, ip: str) -> None:
        self._esp_ip = ip
        log.info(f"ESP IP updated → {ip}")

    def set_host(self, ip: str) -> dict | None:
        """Tell the ESP which IP address to send sensor data to."""
        body = bytes(int(b) for b in ip.split("."))
        self._send(CFG_SET_HOST, body)
        ack = self._recv_ack()
        if ack:
            log.info(f"SET_HOST {ip} → OK  (ESP confirms host={ack['host']})")
        return ack

    def set_simple(self, slot: int, enabled: bool, rate_us: int) -> dict | None:
        """Enable or disable a simple slot and set its sample rate."""
        self._send(CFG_SET_SIMPLE, struct.pack("<BBI", slot, int(enabled), rate_us))
        ack = self._recv_ack()
        if ack:
            hz = round(1e6 / rate_us, 1) if rate_us else 0
            log.info(
                f"SET_SIMPLE slot={slot} {'ON' if enabled else 'OFF'} {hz}Hz → OK"
            )
        return ack

    def set_super(
        self,
        slot:       int,
        dep_slots:  list[int],
        skip_ratio: int = 1,
    ) -> dict | None:
        """Create or replace a super slot."""
        body = struct.pack("<BBB", slot, len(dep_slots), skip_ratio) + bytes(dep_slots)
        self._send(CFG_SET_SUPER, body)
        ack = self._recv_ack()
        if ack:
            names = [SLOT_NAME[d] for d in dep_slots if d < len(SLOT_NAME)]
            log.info(f"SET_SUPER slot={slot} deps={names} skip={skip_ratio} → OK")
        return ack

    def del_super(self, slot: int) -> dict | None:
        """Delete a super slot."""
        self._send(CFG_DEL_SUPER, struct.pack("<B", slot))
        ack = self._recv_ack()
        if ack:
            log.info(f"DEL_SUPER slot={slot} → OK")
        return ack

    def get_state(self) -> dict | None:
        """Request the full ESP state, update the layout, and return parsed state."""
        self._send(CFG_GET_STATE)
        return self._recv_ack()

    def _send(self, cfg_type: int, body: bytes = b"") -> None:
        hdr = CFG_HDR.pack(1, cfg_type, CFG_HDR.size + len(body))
        self._sock.sendto(hdr + body, (self._esp_ip, self._config_port))

    def _recv_ack(self) -> dict | None:
        """Wait for a CFG_ACK datagram and return the parsed state, or None on timeout."""
        try:
            data, _ = self._sock.recvfrom(512)
        except socket.timeout:
            log.warning("Timeout — no response from ESP")
            return None

        if len(data) < DATA_HDR.size:
            log.warning(f"ACK too short: {len(data)} bytes")
            return None

        _, pkt_type, _, _, _ = DATA_HDR.unpack_from(data)
        if pkt_type != 0x30:
            log.warning(f"Unexpected response type: 0x{pkt_type:02X}")
            return None

        return self._parse_ack(data)

    def _parse_ack(self, data: bytes) -> dict:
        """
        Deserialise a CFG_ACK datagram into a state dict and update the layout.

        After this call, SuperSlotLayout reflects the current ESP configuration,
        so the UDP receiver will immediately decode subsequent super-slot packets
        into named fields.
        """
        off = DATA_HDR.size

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

        state = dict(simples=simples, supers=supers, host=host)

        # Propagate to the shared layout so the UDP receiver decodes named fields
        if self._layout is not None:
            self._layout.update(state)

        # Cache as the last-known ESP state (exposed in the panel snapshot).
        self.state = state

        self._log_state(state)
        return state

    @staticmethod
    def _log_state(state: dict) -> None:
        """Log a human-readable summary of the received ESP state."""
        log.info(f"  host_ip = {state['host']}")
        for s in state["simples"]:
            status = "ON " if s["enabled"] else "off"
            name   = SLOT_NAME[s["slot"]] if s["slot"] < len(SLOT_NAME) else "?"
            log.info(f"  simple[{s['slot']}] {status}  {s['rate_hz']:5.0f} Hz  {name}")
        for s in state["supers"]:
            if s["active"]:
                names = [SLOT_NAME[d] for d in s["deps"] if d < len(SLOT_NAME)]
                log.info(
                    f"  super [{s['slot']}] deps={names} "
                    f"skip={s['skip_ratio']} payload={s['payload_sz']}B"
                )
