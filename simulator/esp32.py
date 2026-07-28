"""
simulator/esp32.py — A fake ESP32 speaking the real binary protocol over UDP.

Impersonates the firmware closely enough that the orchestrator cannot tell the
difference below the socket: it holds the same persistent slot configuration,
answers every config command with a full CFG_ACK, and streams sensor packets at
the configured rates to whatever host SET_HOST names.

Two UDP channels, same split as the real device:
    config  (SIM_CONFIG_PORT)   PC → sim commands, ACK sent back to the sender
    data    (UDP_PORT)          sim → PC sensor packets and heartbeat

Because the ACK is what populates the orchestrator's SuperSlotLayout, super
packets get decoded into named fields (gyro_x, game_rv_qw, …) exactly as they
do with real hardware.

Timing: each active stream owns an asyncio task looping on absolute deadlines,
so the rate does not drift. asyncio.sleep resolution caps faithful emission at
roughly 200–500 Hz on macOS, which is well above the 100–200 Hz in normal use.

Nominal behaviour only — no fault injection. Stopping the process is already
enough to exercise the offline path, since the heartbeat simply ceases.
"""

import asyncio
import logging
import math
import socket
import struct
import time
from dataclasses import dataclass, field

import config
from simulator import wire
from simulator.motion import WheelMotion
from transport.protocol import (
    CFG_DEL_SUPER, CFG_GET_STATE, CFG_HEADER, CFG_SET_HOST,
    CFG_SET_SIMPLE, CFG_SET_SUPER, SLOT_FLOAT_COUNT, SLOT_NAME,
)

log = logging.getLogger("simulator")

# BNO08x SH-2 report IDs per simple slot. Cosmetic — the panel only shows them
# as hex; `pkt_type` is the field that actually drives behaviour downstream.
SENSOR_ID = [0x02, 0x01, 0x03, 0x04, 0x05, 0x09, 0x08, 0x28]

# Slot indices, for readability at the call sites below.
SLOT_GYRO, SLOT_GAME_RV = 0, 6


@dataclass
class SimpleSlot:
    """One simple sensor slot, mirroring the firmware's NVS-persisted entry."""

    slot:    int
    enabled: bool = False
    rate_us: int  = 10_000

    @property
    def pkt_type(self) -> int:
        # Slot n reports as packet type n+1 (0x01–0x08). EspHealth maps the
        # ACK's pkt_type back through TYPE_NAME, so this must stay exact.
        return self.slot + 1

    @property
    def payload_sz(self) -> int:
        return SLOT_FLOAT_COUNT[self.slot] * 4

    @property
    def rate_hz(self) -> float:
        return 1e6 / self.rate_us if self.rate_us else 0.0

    def as_ack_entry(self) -> tuple:
        return (self.slot, SENSOR_ID[self.slot], self.pkt_type,
                self.payload_sz, self.enabled, self.rate_us)


@dataclass
class SuperSlot:
    """One super slot: several simple payloads bundled into a single packet."""

    slot:       int
    active:     bool = False
    deps:       list[int] = field(default_factory=list)
    skip_ratio: int = 1

    @property
    def pkt_type(self) -> int:
        return 0x10 + self.slot

    @property
    def payload_sz(self) -> int:
        return sum(SLOT_FLOAT_COUNT[d] for d in self.deps) * 4

    def as_ack_entry(self) -> tuple:
        return (self.slot, self.pkt_type, self.active,
                self.deps, self.skip_ratio, self.payload_sz)


def default_slots(rate_hz: float = 100.0) -> tuple[list[SimpleSlot], list[SuperSlot]]:
    """
    Boot configuration, as if restored from NVS on a device already set up for
    the torus pipeline: GYRO + GAME_RV enabled, super 0 = [GYRO, GAME_RV].
    That is exactly what pipeline/torus_position.py expects to receive.
    """
    rate_us = int(1e6 / rate_hz)
    simples = [SimpleSlot(slot=i) for i in range(8)]
    simples[SLOT_GYRO]    = SimpleSlot(SLOT_GYRO,    enabled=True, rate_us=rate_us)
    simples[SLOT_GAME_RV] = SimpleSlot(SLOT_GAME_RV, enabled=True, rate_us=rate_us)

    supers = [SuperSlot(slot=i) for i in range(8)]
    supers[0] = SuperSlot(0, active=True, deps=[SLOT_GYRO, SLOT_GAME_RV], skip_ratio=1)
    return simples, supers


class _ConfigProtocol(asyncio.DatagramProtocol):
    """Receives config commands and hands them to the simulator."""

    def __init__(self, sim: "Esp32Simulator"):
        self._sim = sim

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        self._sim.handle_command(data, addr)

    def error_received(self, exc: Exception) -> None:
        log.error(f"Config socket error: {exc}")


class Esp32Simulator:
    """
    Fake ESP32: config state machine + sensor stream emitter.

    Usage:
        sim = Esp32Simulator(WheelMotion("coin"))
        await sim.start()
        ...
        await sim.stop()
    """

    def __init__(
        self,
        motion:            WheelMotion,
        config_port:       int   = config.SIM_CONFIG_PORT,
        data_host:         str   = "127.0.0.1",
        data_port:         int   = config.UDP_PORT,
        rate_hz:           float = 100.0,
        heartbeat_hz:      float = 1.0,
        emit_dep_simples:  bool  = True,
        truth_interval_s:  float = 0.0,
    ):
        self.motion   = motion
        # What each simple slot reads, in slot order. RV / GEO_RV / GAME_RV /
        # ARVR_RV differ only by reference frame on real hardware; the model has
        # a single attitude, so all four report the same quaternion.
        self._readers = (
            motion.gyro, motion.accel, motion.mag, motion.linear_accel,
            motion.quaternion, motion.quaternion,
            motion.quaternion, motion.quaternion,
        )
        self.simples, self.supers = default_slots(rate_hz)
        self.host_ip  = data_host
        self._cfg_port      = config_port
        self._data_port     = data_port
        self._hb_period     = 1.0 / heartbeat_hz if heartbeat_hz > 0 else 0.0
        self._emit_deps     = emit_dep_simples
        self._truth_every   = truth_interval_s

        self._boot          = time.monotonic()
        self._seq: dict[int, int] = {}     # per packet type
        self._packets_sent  = 0
        self._udp_errors    = 0

        self._data_sock: socket.socket | None = None
        self._cfg_transport = None
        self._streams: list[asyncio.Task] = []
        # Serialises stream rebuilds: commands can arrive back to back, and two
        # concurrent rebuilds would each cancel then re-add, doubling the tasks.
        self._restart_lock = asyncio.Lock()

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Bind both sockets and spawn the emitter tasks."""
        loop = asyncio.get_running_loop()
        self._data_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._cfg_transport, _ = await loop.create_datagram_endpoint(
            lambda: _ConfigProtocol(self),
            local_addr=("0.0.0.0", self._cfg_port),
        )
        log.info(
            f"Fake ESP32 up — config on :{self._cfg_port}, "
            f"data → {self.host_ip}:{self._data_port}, "
            f"scenario '{self.motion.scenario}' ({self.motion.reference_note()})"
        )
        self._respawn_streams()

    async def stop(self) -> None:
        """Cancel the emitters and close both sockets."""
        async with self._restart_lock:
            await self._cancel_streams()
        if self._cfg_transport is not None:
            self._cfg_transport.close()
            self._cfg_transport = None
        if self._data_sock is not None:
            self._data_sock.close()
            self._data_sock = None
        log.info("Fake ESP32 stopped")

    # ── Clock and emission ───────────────────────────────────────────────────

    def _now(self) -> float:
        """Seconds since simulated boot."""
        return time.monotonic() - self._boot

    def _next_seq(self, type_id: int) -> int:
        n = self._seq.get(type_id, 0)
        self._seq[type_id] = n + 1
        return n

    def _send(self, datagram: bytes) -> None:
        if self._data_sock is None:
            return
        try:
            self._data_sock.sendto(datagram, (self.host_ip, self._data_port))
            self._packets_sent += 1
        except OSError as e:
            self._udp_errors += 1
            log.debug(f"send failed: {e}")

    def _slot_values(self, slot: int, t: float) -> tuple:
        """The float payload a given simple slot reports at time `t`."""
        return self._readers[slot](t)

    def _emit_simple(self, slot: int) -> None:
        t      = self._now()
        ts_us  = int(t * 1e6)
        s      = self.simples[slot]
        values = self._slot_values(slot, t)
        if SLOT_FLOAT_COUNT[slot] == 3:
            pkt = wire.build_vec3(s.pkt_type, self._next_seq(s.pkt_type), ts_us, values)
        else:
            pkt = wire.build_quat(s.pkt_type, self._next_seq(s.pkt_type), ts_us, values)
        self._send(pkt)

    def _emit_super(self, slot: int) -> None:
        t     = self._now()
        ts_us = int(t * 1e6)
        sup   = self.supers[slot]
        # Concatenate dep payloads in dep order — that order is what the
        # receiver replays through SLOT_FIELDS to name the fields.
        values: list[float] = []
        for dep in sup.deps:
            values.extend(self._slot_values(dep, t))
        self._send(wire.build_super(slot, self._next_seq(sup.pkt_type), ts_us, values))

    def _emit_heartbeat(self) -> None:
        t     = self._now()
        ts_us = int(t * 1e6)
        # Plausible, slowly-varying telemetry so the panel's fields look alive.
        rssi    = int(-55 + 4 * math.sin(t / 7.0))
        temp    = 45.0 + 2.0 * math.sin(t / 11.0)
        battery = max(0.0, 100.0 - t / 60.0)
        self._send(wire.build_heartbeat(
            self._next_seq(0x20), ts_us,
            uptime_ms    = int(t * 1000),
            packets_sent = self._packets_sent,
            udp_errors   = self._udp_errors,
            rssi_dbm     = rssi,
            cpu_temp_c   = temp,
            battery_pct  = battery,
        ))

    def _log_truth(self) -> None:
        ref = self.motion.reference(self._now())
        px  = "—" if ref["px"] is None else f"{ref['px']:+.3f}"
        py  = "—" if ref["py"] is None else f"{ref['py']:+.3f}"
        log.info(f"ground truth  px={px}  py={py}  pz={ref['pz']:.3f}")

    # ── Stream scheduling ────────────────────────────────────────────────────

    async def _run_stream(
        self, period_s: float, emit, label: str, announce: bool = True
    ) -> None:
        """Emit at a fixed period, aiming at absolute deadlines to avoid drift."""
        if announce:
            log.info(f"stream {label} @ {1 / period_s:.1f} Hz")
        next_t = time.monotonic()
        while True:
            next_t += period_s
            delay = next_t - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            else:
                next_t = time.monotonic()   # fell behind — resync rather than burst
            emit()

    async def _cancel_streams(self) -> None:
        for task in self._streams:
            task.cancel()
        if self._streams:
            await asyncio.gather(*self._streams, return_exceptions=True)
        self._streams.clear()

    def _respawn_streams(self) -> None:
        """
        (Re)build the emitter tasks from the current configuration.

        Called at boot and after every config command, so a rate change made
        from the control panel shows up in LiveMonitor immediately.
        """
        asyncio.ensure_future(self._restart_streams())

    async def _restart_streams(self) -> None:
        async with self._restart_lock:
            await self._cancel_streams()

            dep_slots = {d for sup in self.supers if sup.active for d in sup.deps}

            for s in self.simples:
                if not s.enabled or s.rate_us <= 0:
                    continue
                if s.slot in dep_slots and not self._emit_deps:
                    continue
                self._streams.append(asyncio.ensure_future(self._run_stream(
                    s.rate_us / 1e6,
                    lambda slot=s.slot: self._emit_simple(slot),
                    SLOT_NAME[s.slot],
                )))

            for sup in self.supers:
                if not sup.active or not sup.deps:
                    continue
                dep_rates = [self.simples[d].rate_hz for d in sup.deps
                             if self.simples[d].enabled]
                if len(dep_rates) != len(sup.deps) or not all(dep_rates):
                    log.warning(
                        f"super {sup.slot}: deps {sup.deps} not all enabled "
                        "— not streaming"
                    )
                    continue
                # A super fires at its slowest dep's rate, decimated by
                # skip_ratio — the same formula EspHealth uses to derive the
                # expected rate, so measured and expected agree.
                hz = min(dep_rates) / max(1, sup.skip_ratio)
                self._streams.append(asyncio.ensure_future(self._run_stream(
                    1.0 / hz,
                    lambda slot=sup.slot: self._emit_super(slot),
                    f"SUPER_{sup.slot}",
                )))

            if self._hb_period:
                self._streams.append(asyncio.ensure_future(self._run_stream(
                    self._hb_period, self._emit_heartbeat, "HEARTBEAT",
                )))

            if self._truth_every:
                self._streams.append(asyncio.ensure_future(self._run_stream(
                    self._truth_every, self._log_truth, "truth", announce=False,
                )))

    # ── Config state machine ─────────────────────────────────────────────────

    def handle_command(self, data: bytes, addr: tuple) -> None:
        """
        Apply one config command and reply with the full state.

        The ACK always goes back to the datagram's source address, which is how
        the real firmware answers and why EspConfigurator receives it on the
        port it sent from.
        """
        if len(data) < CFG_HEADER.size:
            log.warning(f"Command too short: {len(data)} bytes")
            return

        _, cmd, _ = CFG_HEADER.unpack_from(data)
        body      = data[CFG_HEADER.size:]
        changed   = True

        try:
            if cmd == CFG_SET_SIMPLE:
                slot, enabled, rate_us = struct.unpack("<BBI", body)
                s = self.simples[slot]
                s.enabled = bool(enabled)
                if rate_us:
                    s.rate_us = rate_us
                log.info(f"SET_SIMPLE slot={slot} "
                         f"{'ON' if s.enabled else 'OFF'} {s.rate_hz:.1f} Hz")

            elif cmd == CFG_SET_SUPER:
                slot, n_deps, skip = struct.unpack_from("<BBB", body)
                deps = list(body[3:3 + n_deps])
                self.supers[slot] = SuperSlot(slot, True, deps, max(1, skip))
                names = [SLOT_NAME[d] for d in deps if d < len(SLOT_NAME)]
                log.info(f"SET_SUPER slot={slot} deps={names} skip={skip}")

            elif cmd == CFG_DEL_SUPER:
                slot = body[0]
                self.supers[slot] = SuperSlot(slot)
                log.info(f"DEL_SUPER slot={slot}")

            elif cmd == CFG_SET_HOST:
                self.host_ip = ".".join(str(b) for b in body[:4])
                log.info(f"SET_HOST → {self.host_ip}")

            elif cmd == CFG_GET_STATE:
                changed = False
                log.info("GET_STATE")

            else:
                log.warning(f"Unknown command 0x{cmd:02X}")
                return

        except (struct.error, IndexError, KeyError) as e:
            log.warning(f"Malformed command 0x{cmd:02X}: {e}")
            return

        self._reply_ack(addr)
        if changed:
            self._respawn_streams()

    def _reply_ack(self, addr: tuple) -> None:
        ack = wire.build_ack(
            [s.as_ack_entry() for s in self.simples],
            [s.as_ack_entry() for s in self.supers],
            self.host_ip,
            self._next_seq(0x30),
            int(self._now() * 1e6),
        )
        if self._cfg_transport is not None:
            self._cfg_transport.sendto(ack, addr)


async def start_simulator(
    scenario: str = "coin",
    **kwargs,
) -> Esp32Simulator:
    """
    Convenience constructor used by core.startup() when SIM_EMBEDDED is set.

    Keyword arguments are split between the motion model and the simulator.
    """
    motion_keys = ("lean_deg", "spin_dps", "precession_dps", "spiral_period_s")
    motion_kw   = {k: kwargs.pop(k) for k in list(kwargs) if k in motion_keys}
    sim = Esp32Simulator(WheelMotion(scenario, **motion_kw), **kwargs)
    await sim.start()
    return sim
