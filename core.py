"""
core.py — Shared orchestrator wiring.

Holds the singleton services (configurator, session/CSV/playback, layout) and
the packet-processing tasks that used to live in main.py.  Both the FastAPI
lifespan (api/app.py) and the route handlers (api/routes.py) import the same
instances from here, so there is a single source of truth for runtime state.

Lifecycle:
  await startup()    — boots WS server, UDP receiver, configurator; launches
                       processing_loop + log_stats; populates the layout via
                       SET_HOST.  Exposes `queue`, `ws_server`, `udp_protocol`.
  await shutdown()   — cancels tasks, stops any recording/playback, closes the
                       configurator socket.
"""

import asyncio
import logging
import socket as _socket

import config
from transport.super_layout     import SuperSlotLayout
from transport.udp_receiver     import start_udp_receiver
from transport.esp_configurator import EspConfigurator
from transport.ws_server        import WSServer
from transport.live_monitor     import LiveMonitor
from transport.esp_health        import EspHealth
from transport.protocol         import HB_TYPE
from pipeline.torus_position    import TorusPositionStage
from storage.session_manager    import SessionManager
from storage.csv_logger         import CSVLogger
from storage.playback_engine    import PlaybackEngine

log = logging.getLogger("core")


def _local_ip() -> str:
    """Detect the active local IP by opening a dummy UDP connection."""
    if config.SIM_ENABLED:
        # The fake ESP32 lives on this machine; announcing the LAN address
        # would work but needlessly breaks when there is no network at all.
        return "127.0.0.1"
    s = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
    s.connect(("8.8.8.8", 80))
    ip = s.getsockname()[0]
    s.close()
    return ip


# Shared layout — written by EspConfigurator, read by UDPReceiver's parser
layout = SuperSlotLayout()

# Pipeline stages — executed in order for every received packet.
# Add a new stage here and create its module under pipeline/.
PIPELINE_STAGES = [
    TorusPositionStage(),
    # CalibrationStage(),   ← iteration 4
]

session_manager = SessionManager()
csv_logger      = CSVLogger(session_manager)
playback_engine = PlaybackEngine(session_manager)

# Observes the packet stream (rates, latest values, liveness) for GET /api/live.
monitor = LiveMonitor()

configurator = EspConfigurator(
    esp_host    = config.ESP_HOST,
    config_port = config.CONFIG_PORT,
    local_port  = config.CONFIG_LOCAL_PORT,
    timeout     = config.CONFIG_ACK_TIMEOUT_S,
    layout      = layout,
)

# Unified ESP health: fuses heartbeat presence/telemetry with stream
# conformance (measured rates vs the configured ESP state). Single UI verdict.
esp_health = EspHealth(
    monitor, configurator,
    heartbeat_timeout_s = config.HEARTBEAT_TIMEOUT_S,
    rate_tolerance      = config.RATE_TOLERANCE,
)

# Runtime handles — populated by startup(), referenced by the API routes.
queue:        asyncio.Queue | None = None
ws_server:    WSServer | None      = None
udp_protocol = None
_transport   = None
_simulator   = None                  # fake ESP32, only when config.SIM_EMBEDDED
_tasks: list[asyncio.Task] = []


def accept_live(packet: dict) -> bool:
    """
    Admission gate for live UDP packets — playback owns the pipeline.

    The queue has two producers (UDPReceiver and PlaybackEngine) and they carry
    unrelated `ts_esp_us` time bases.  Interleaved, they make the dt that
    TorusPositionStage derives from that field meaningless and its Euler
    integration of px/py diverges.  So a running replay is exclusive: live
    sensor packets are dropped at the socket for its whole duration.

    The heartbeat is exempt.  It is live-only telemetry (never written to the
    CSV, hence never replayed), it carries no ts-based state, and without it
    EspHealth would report the ESP offline a few seconds into every replay.
    """
    return not playback_engine.active or packet.get("typeId") == HB_TYPE


async def processing_loop(q: asyncio.Queue, ws: WSServer) -> None:
    """
    Main packet consumer loop.

    For each packet dequeued:
      1. Observe it (live metrics) and write to CSV (raw, before pipeline)
      2. Run through all pipeline stages (a None result drops the packet)
      3. Observe computed output, then broadcast the enriched packet over WS
    """
    log.info("Processing loop started")
    while True:
        packet = await q.get()

        if packet.get("typeId") == "playback_end":
            # Reset here, not in PlaybackEngine: the sentinel is the point in the
            # *stream* where the replay ends, so stateful stages are cleared
            # before the live packets queued behind it are integrated. Resetting
            # only at the start of a replay pass (as PlaybackEngine does) would
            # leave the take's final px/py as the live mode's starting offset.
            log.info("Playback session ended — returning to IDLE")
            for stage in PIPELINE_STAGES:
                await stage.reset()
            q.task_done()
            continue

        monitor.observe(packet)
        csv_logger.write(packet)

        for stage in PIPELINE_STAGES:
            if packet is None:
                break
            try:
                packet = await stage.process(packet)
            except Exception as e:
                log.error(f"Error in {stage.__class__.__name__}: {e}")
                packet = None

        if packet is not None:
            if "px" in packet:           # computed torus-position output
                monitor.observe(packet)
            await ws.broadcast(packet)

        q.task_done()


async def log_stats(interval: float, q: asyncio.Queue, udp_proto, ws: WSServer) -> None:
    """Log a periodic status line with queue depth, packet counts, and client count."""
    while True:
        await asyncio.sleep(interval)

        # Self-heal the ESP target from the data plane: the source IP of incoming
        # sensor packets is the ESP's real address. Adopt it whenever it differs
        # from our current target (mDNS miss, stale resolve, or a mid-séance DHCP
        # change), and SET_HOST once if we never reached the ESP — that ACK also
        # populates the super-slot layout so named decoding kicks in.
        rx_ip = udp_proto.last_esp_ip
        if rx_ip and rx_ip != configurator.esp_ip:
            never_acked = configurator.state is None
            configurator.esp_ip = rx_ip
            if never_acked:
                await asyncio.to_thread(configurator.set_host, _local_ip())

        mode = "REC" if csv_logger.active else ("PLAY" if playback_engine.active else "IDLE")
        log.info(
            f"[{mode}]  Queue:{q.qsize()}  "
            f"UDP rx:{udp_proto.stats['rx']} err:{udp_proto.stats['errors']}  "
            f"WS tx:{ws.stats['tx']} clients:{len(ws.clients)}"
        )


def current_mode() -> str:
    """Return the orchestrator mode: REC, PLAY, or IDLE."""
    if csv_logger.active:
        return "REC"
    if playback_engine.active:
        return "PLAY"
    return "IDLE"


# ── Snapshot builders — single source of truth for the panel state ───────────
# Shared by the REST observation endpoints (fallback) and the WS push channel.

def status_dict() -> dict:
    """Orchestrator status: mode, queue depth, UDP/WS counters."""
    return {
        "mode":        current_mode(),
        "queue_depth": queue.qsize() if queue else 0,
        "udp": {
            "rx":          udp_protocol.stats["rx"]     if udp_protocol else 0,
            "errors":      udp_protocol.stats["errors"] if udp_protocol else 0,
            # Live packets dropped at the socket because a replay owns the
            # pipeline (see accept_live) — grows only during playback.
            "muted":       udp_protocol.stats["muted"]  if udp_protocol else 0,
            "last_esp_ip": udp_protocol.last_esp_ip     if udp_protocol else None,
        },
        "ws": {
            "tx":      ws_server.stats["tx"]   if ws_server else 0,
            "clients": len(ws_server.clients) if ws_server else 0,
            # Packets dropped for a client that could not keep up (drop-oldest
            # fan-out). Non-zero means a downstream viewer is lagging — the
            # pipeline itself is never held back for it.
            "dropped": ws_server.stats["dropped"] if ws_server else 0,
        },
        "esp_net": {
            "hostname": configurator.hostname,
            "ip":       configurator.esp_ip,
            "resolved": configurator.resolved,
        },
    }


def session_dict() -> dict | None:
    """Active session metadata with its takes, or None when no session is open."""
    return session_manager.active_tree()


def recording_dict() -> dict:
    """Current take-recording state."""
    meta = csv_logger._meta
    return {
        "active":       csv_logger.active,
        "take":         meta.name if meta else None,
        "title":        meta.title if meta else None,
        "packet_count": meta.packet_count if meta else 0,
    }


def playback_dict() -> dict:
    """Current playback state with progress."""
    pb = playback_engine
    percent = round(100 * pb.index / pb.total, 1) if pb.total else 0.0
    return {
        "active":    pb.active,
        "paused":    pb.paused,
        "session":   pb.session,
        "take":      pb.take,
        "index":     pb.index,
        "total":     pb.total,
        "percent":   percent,
        "elapsed_s": round(pb.elapsed_s, 1),
        "total_s":   round(pb.total_s, 1),
        "speed":     pb.speed,
        "loop":      pb.loop,
    }


def panel_snapshot() -> dict:
    """Full observation snapshot pushed to the control panel over WS."""
    return {
        "status":    status_dict(),
        "live":      monitor.snapshot(),
        "health":    esp_health.snapshot(),
        "session":   session_dict(),
        "recording": recording_dict(),
        "playback":  playback_dict(),
        "esp":       configurator.state,
    }


async def startup() -> None:
    """Boot all subsystems and launch the background tasks."""
    global queue, ws_server, udp_protocol, _transport, _simulator

    queue = asyncio.Queue()

    ws_server = WSServer(config.WS_HOST, config.WS_PORT)
    await ws_server.start()

    _transport, udp_protocol = await start_udp_receiver(
        config.UDP_HOST, config.UDP_PORT, queue, layout, accept_live
    )

    # Dev mode: run a fake ESP32 in-process. It must be listening before the
    # configurator's startup SET_HOST, since that ACK is what populates the
    # super-slot layout. Imported here so production never loads the package.
    if config.SIM_EMBEDDED:
        from simulator import start_simulator
        _simulator = await start_simulator(config.SIM_SCENARIO)
        log.warning("SIMULATOR MODE — talking to a fake ESP32, not real hardware")

    configurator.start()
    my_ip = _local_ip()
    log.info(f"Local IP: {my_ip}")
    # Resolve the ESP's mDNS hostname (imu-cyrwheel.local) to its current IP
    # instead of relying on a hardcoded address. On failure we don't SET_HOST —
    # log_stats will adopt the ESP's address from incoming packets instead.
    if await asyncio.to_thread(configurator.resolve):
        # SET_HOST also populates the layout via the ACK, so super packets are
        # decoded into named fields immediately after this call returns.
        await asyncio.to_thread(configurator.set_host, my_ip)
    else:
        log.warning(
            f"ESP not reachable at {config.ESP_HOST} — skipping SET_HOST; "
            "will adopt its address from incoming sensor data."
        )

    _tasks.append(asyncio.ensure_future(processing_loop(queue, ws_server)))
    _tasks.append(asyncio.ensure_future(log_stats(30.0, queue, udp_protocol, ws_server)))

    log.info("Orchestrator ready")


async def shutdown() -> None:
    """Tear down all subsystems cleanly."""
    global _simulator

    if csv_logger.active:
        csv_logger.stop()
    if playback_engine.active:
        playback_engine.stop()

    for task in _tasks:
        task.cancel()
    _tasks.clear()

    if _transport is not None:
        _transport.close()

    if _simulator is not None:
        await _simulator.stop()
        _simulator = None

    configurator.stop()
    log.info("Orchestrator shut down")
