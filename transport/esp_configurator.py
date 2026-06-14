"""
transport/esp_configurator.py — UDP config client for the ESP32.

Sends configuration commands to the ESP32 on the config port (4211) and
parses the full-state ACK returned after each command.

After each ACK, the shared SuperSlotLayout is updated so that the UDP
receiver can immediately decode subsequent super-slot packets into properly
named fields.

The wire format (command bodies, ACK layout, struct definitions) lives in
`transport/protocol.py`; this module only owns the socket and the connection
state.  Commands are built with `protocol.build_*` and the reply is parsed
with `protocol.parse_ack`.

Each command flushes any stale/late ACK still buffered before sending, and
reads the freshest reply, so the displayed state never lags a command behind
(important because the firmware can take a few seconds to answer).

All public methods are blocking (`timeout` seconds, default from
config.CONFIG_ACK_TIMEOUT_S).
Call from asyncio via: await loop.run_in_executor(None, cfg.method, ...)
"""

import socket
import logging

from transport import protocol
from transport.protocol import SLOT_NAME
from transport.super_layout import SuperSlotLayout

log = logging.getLogger("esp_configurator")


def _is_ipv4(s: str) -> bool:
    """True if `s` is already a dotted-quad IPv4 literal (no resolution needed)."""
    try:
        socket.inet_aton(s)
        return s.count(".") == 3
    except OSError:
        return False


class EspConfigurator:
    """
    UDP configuration client for the ESP32.

    Accepts an optional SuperSlotLayout reference; the layout is updated
    automatically after every ACK so that the UDP receiver can decode
    super-slot packets into named fields without delay.

    The ESP is addressed by hostname (`esp_host`, typically the mDNS name
    "imu-cyrwheel.local") rather than a fixed IP; call resolve() to look it up.

    Typical usage:
        layout = SuperSlotLayout()
        cfg = EspConfigurator("imu-cyrwheel.local", 4211, 4211, layout=layout)
        cfg.start()
        cfg.resolve()                  # mDNS hostname → IP (cached as send target)
        state = cfg.get_state()        # also populates layout
        cfg.set_simple(slot=0, enabled=True, rate_us=20_000)
        cfg.stop()
    """

    def __init__(
        self,
        esp_host:    str,
        config_port: int,
        local_port:  int,
        timeout:     float = 2.0,
        layout:      SuperSlotLayout | None = None,
    ):
        self._esp_host    = esp_host        # mDNS hostname or literal IP from config
        self._esp_ip      = esp_host        # send target; == host until resolve() runs
        self._resolved    = _is_ipv4(esp_host)  # a literal IP needs no resolution
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
            f"Configurator ready → {self._esp_host}:{self._config_port} "
            f"(local port {self._local_port})"
        )

    def stop(self) -> None:
        """Close the UDP socket."""
        if self._sock:
            self._sock.close()
            self._sock = None

    @property
    def hostname(self) -> str:
        """The configured ESP host (mDNS name or literal IP)."""
        return self._esp_host

    @property
    def esp_ip(self) -> str:
        """The address commands are currently sent to (resolved IP, or the host)."""
        return self._esp_ip

    @esp_ip.setter
    def esp_ip(self, ip: str) -> None:
        if ip != self._esp_ip:
            log.info(f"ESP target updated → {ip}")
        self._esp_ip   = ip
        self._resolved = True

    @property
    def resolved(self) -> bool:
        """True once the host maps to a concrete IP (via resolve or the data plane)."""
        return self._resolved

    def resolve(self) -> str | None:
        """
        Resolve the configured ESP hostname to an IPv4 address and cache it as
        the send target.  On macOS, `.local` names go through Bonjour/mDNS.

        Returns the IP on success, or None on failure (the host string is left
        as the target, so a literal IP keeps working and the data-plane fallback
        in core.log_stats can still adopt the address from incoming packets).

        Blocking (the OS resolver can stall a few seconds on a miss); call via
        asyncio.to_thread from the event loop.
        """
        try:
            info = socket.getaddrinfo(
                self._esp_host, self._config_port,
                family=socket.AF_INET, type=socket.SOCK_DGRAM,
            )
            ip = info[0][4][0]
        except socket.gaierror as e:
            log.warning(f"Could not resolve ESP host {self._esp_host!r}: {e}")
            return None
        if ip != self._esp_ip:
            log.info(f"Resolved {self._esp_host} → {ip}")
        self._esp_ip   = ip
        self._resolved = True
        return ip

    def set_host(self, ip: str) -> dict | None:
        """Tell the ESP which IP address to send sensor data to."""
        self._send(protocol.build_set_host(ip))
        ack = self._recv_ack()
        if ack:
            log.info(f"SET_HOST {ip} → OK  (ESP confirms host={ack['host']})")
        return ack

    def set_simple(self, slot: int, enabled: bool, rate_us: int) -> dict | None:
        """Enable or disable a simple slot and set its sample rate."""
        self._send(protocol.build_set_simple(slot, enabled, rate_us))
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
        self._send(protocol.build_set_super(slot, dep_slots, skip_ratio))
        ack = self._recv_ack()
        if ack:
            names = [SLOT_NAME[d] for d in dep_slots if d < len(SLOT_NAME)]
            log.info(f"SET_SUPER slot={slot} deps={names} skip={skip_ratio} → OK")
        return ack

    def del_super(self, slot: int) -> dict | None:
        """Delete a super slot."""
        self._send(protocol.build_del_super(slot))
        ack = self._recv_ack()
        if ack:
            log.info(f"DEL_SUPER slot={slot} → OK")
        return ack

    def get_state(self) -> dict | None:
        """Request the full ESP state, update the layout, and return parsed state."""
        self._send(protocol.build_get_state())
        return self._recv_ack()

    def _send(self, datagram: bytes) -> None:
        # Drop any datagram still buffered from a previous exchange (e.g. a late
        # ACK that arrived after we had already timed out) so the reply we read
        # next corresponds to the command we are about to send, not a stale one.
        dropped = self._flush()
        if dropped:
            log.debug(f"Discarded {dropped} stale datagram(s) before send")
        self._sock.sendto(datagram, (self._esp_ip, self._config_port))

    def _flush(self) -> int:
        """Discard all datagrams currently buffered on the socket. Non-blocking."""
        self._sock.settimeout(0)
        dropped = 0
        try:
            while True:
                try:
                    self._sock.recv(512)
                    dropped += 1
                except (BlockingIOError, socket.timeout):
                    break
        finally:
            self._sock.settimeout(self._timeout)
        return dropped

    def _recv_ack(self) -> dict | None:
        """
        Wait for a CFG_ACK and return the parsed state, or None on timeout.

        Blocks up to `self._timeout` for the first reply, then non-blockingly
        drains any further replies already queued and keeps the most recent —
        so a late ACK from an earlier command can never make us display stale
        state.
        """
        try:
            data, _ = self._sock.recvfrom(512)
        except socket.timeout:
            log.warning("Timeout — no response from ESP")
            return None

        # Keep only the freshest datagram if several are already buffered.
        self._sock.settimeout(0)
        try:
            while True:
                try:
                    data, _ = self._sock.recvfrom(512)
                except (BlockingIOError, socket.timeout):
                    break
        finally:
            self._sock.settimeout(self._timeout)

        if len(data) < protocol.DATA_HEADER_SIZE:
            log.warning(f"ACK too short: {len(data)} bytes")
            return None

        _, pkt_type, _, _, _ = protocol.DATA_HEADER.unpack_from(data)
        if pkt_type != protocol.ACK_TYPE:
            log.warning(f"Unexpected response type: 0x{pkt_type:02X}")
            return None

        # protocol.parse_ack is pure; the side effects (layout sync, state
        # cache, logging) belong to this stateful client.
        state = protocol.parse_ack(data)

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
