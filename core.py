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
from model.bus                  import ModelBus
from model.engine               import Model
from model.scope                import ScopeRing
from model.types                import FRAME, META, RAW
from osc.bridge                 import OscBridge
from osc.live                   import LiveLink
from osc.routes                 import RouteTable
from storage.session_manager    import SessionManager
from storage.csv_logger         import CSVLogger
from storage.playback_engine    import PlaybackEngine
from storage.pose_track         import PoseTrackService

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

# The single fan-out point between what we compute and every output. The WS
# server is one subscriber; the scope ring, the event log and (later) the OSC
# bridge are others. Nothing downstream is privileged.
bus = ModelBus()

# The interpretation layer. It publishes frames (and later events) on the bus
# itself, so processing_loop only has to hand it packets. Signals are declared
# in model/signals/ — adding one there is the whole wiring.
model: Model  # assigned below, once `configurator` exists to read the ESP state from

# The most recent frame, kept for the 4 Hz panel push. Fed by the bus rather
# than by processing_loop, so the panel is a subscriber like anything else and
# the model never has to know it exists.
latest_frame = None


def _remember_frame(kind: str, frame) -> None:
    global latest_frame
    latest_frame = frame


bus.subscribe_sync("panel", (FRAME,), _remember_frame)

# Full-rate signal history behind the scope. Inline, so it records every frame
# even during a fast replay where the WS fan-out is dropping them on purpose.
scope = ScopeRing()
bus.subscribe_sync("scope", (FRAME, META), scope.on_bus)

# Failures of the engine itself, as opposed to of a single signal (which the
# registry contains). Should stay at zero; if it does not, the model is frozen
# and the panel must say so rather than showing a plausible stale frame.
model_errors = {"count": 0, "last": None}
_MODEL_LOG_EVERY = 200

session_manager = SessionManager()
csv_logger      = CSVLogger(session_manager)
playback_engine = PlaybackEngine(session_manager)

# Precomputed poses beside each take, so a take can be swept without replaying
# it. Its computations run in worker threads and touch nothing here: they drive
# their own isolated Model with no bus, which is what keeps a sweep from firing
# events into Live (see storage/pose_track.py).
pose_tracks = PoseTrackService(session_manager)

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

model = Model(
    bus,
    max_gap_us = int(config.MAX_DT_S * 1e6),
    esp_state  = lambda: configurator.state,
)

# OSC bridge: bus -> Ableton Live, remapped by editing routes, never by
# touching this code (see osc/ package docstring). Constructed here — nothing
# below needs a running event loop — but attached to the bus and its sockets
# started inside startup(), exactly like ws_server: the RELIABLE event
# subscription creates its own draining task, which requires the loop to
# already be running (model/bus.py's subscribe()).
osc_routes = RouteTable()
osc_live   = LiveLink(
    host        = config.OSC_HOST,
    send_port   = config.OSC_SEND_PORT,
    listen_port = config.OSC_LISTEN_PORT,
)
osc_bridge = OscBridge(osc_routes, osc_live, rate_hz = config.OSC_RATE_HZ)

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


async def processing_loop(q: asyncio.Queue) -> None:
    """
    Main packet consumer loop.

    Publishes through the module-level `bus`, the same one the model was built
    with. Taking it as an argument invited two references to the one fan-out
    point, which is one too many.

    For each packet dequeued:
      1. Observe it (live metrics) and write to CSV — raw, before the model, so
         the recording never depends on the computation model of the day
      2. Publish the raw packet: it is the wire, and clients may want it
      3. Feed the model, which publishes its own frame when the packet produced
         a tick

    Nothing here can drop a packet.  The model contains its own failures at the
    node (see model/registry.py), so a broken detector costs its own value and
    never the stream — which used to be false, and is the difference between one
    dead signal and a stuttering visual during a show.
    """
    log.info("Processing loop started")
    while True:
        packet = await q.get()

        if packet.get("typeId") == "playback_end":
            # Reset here, not in PlaybackEngine: the sentinel is the point in the
            # *stream* where the replay ends, so state is cleared before the live
            # packets queued behind it are integrated. Resetting only at the
            # start of a replay pass would leave the take's final position as
            # live mode's starting offset.
            log.info("Playback session ended — returning to IDLE")
            model.reset()
            q.task_done()
            continue

        monitor.observe(packet)
        csv_logger.write(packet)
        bus.publish(RAW, packet)

        try:
            model.feed(packet)
        except Exception as e:
            # The registry already contains a failing *node*, so reaching here
            # means the engine or the resolver itself broke. Letting it escape
            # would kill this task, and with it the only consumer of the queue —
            # the orchestrator would go silently deaf. Recording and the raw
            # stream above are already done, so a show carries on with a frozen
            # model rather than no data at all.
            n = model_errors["count"] = model_errors["count"] + 1
            model_errors["last"] = str(e)
            if n % _MODEL_LOG_EVERY == 1:
                log.exception(f"Model.feed raised ({n} so far)")

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
        w = ws.snapshot()
        log.info(
            f"[{mode}]  Queue:{q.qsize()}  "
            f"UDP rx:{udp_proto.stats['rx']} err:{udp_proto.stats['errors']}  "
            f"WS tx:{w['tx']} clients:{w['clients']} dropped:{w['dropped']}"
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
        # Packets dropped for a client that could not keep up (drop-oldest
        # fan-out). Non-zero means a downstream viewer is lagging — the pipeline
        # itself is never held back for it. `forced` counts events discarded
        # anyway, which is never acceptable and should stay at zero.
        "ws": ws_server.snapshot() if ws_server else {
            "clients": 0, "tx": 0, "errors": 0,
            "dropped": 0, "forced": 0, "backlog": 0,
        },
        "esp_net": {
            "hostname": configurator.hostname,
            "ip":       configurator.esp_ip,
            "resolved": configurator.resolved,
        },
        "bus": bus.stats(),
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


def model_dict() -> dict:
    """
    Model state for the panel: runtime counters plus the latest frame.

    The frame carries the current value of every available signal.  It is a
    convenience for the 4 Hz panel, not the way to watch a signal: at 4 Hz you
    see one sample in twenty-five, which is useless for setting a threshold.
    That is what the scope's history endpoint is for.
    """
    frame = latest_frame
    return {
        **model.snapshot(),
        "pose":    frame.pose if frame else None,
        "signals": frame.signals if frame else {},
        "quality": frame.quality if frame else None,
        "engine_errors": dict(model_errors),
        "scope":   scope.stats(),
    }


def osc_dict() -> dict:
    """OSC bridge runtime: enabled, rate, AbletonOSC link health, counters."""
    return osc_bridge.snapshot()


def osc_routes_dict() -> dict:
    """
    The route table, each row annotated with whether its source still exists.

    Recomputed from the live model on every call rather than cached on the
    route — exactly like a signal's own availability (model/registry.py): a
    route naming a signal that was since removed or renamed still loads and
    lists, it just says why it cannot fire.
    """
    return {
        "routes": osc_routes.schema(
            frozenset(model.registry.names), frozenset(model.detectors.names),
        ),
        "profile":  osc_routes.profile,
        "profiles": osc_routes.list_profiles(),
        "revision": osc_routes.revision,
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
        "model":     model_dict(),
        "osc":       osc_dict(),
    }


async def startup() -> None:
    """Boot all subsystems and launch the background tasks."""
    global queue, ws_server, udp_protocol, _transport, _simulator

    queue = asyncio.Queue()

    ws_server = WSServer(config.WS_HOST, config.WS_PORT)
    await ws_server.start()
    ws_server.attach(bus)

    osc_bridge.attach(bus)
    await osc_live.start()
    await osc_bridge.start()

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

    _tasks.append(asyncio.ensure_future(processing_loop(queue)))
    _tasks.append(asyncio.ensure_future(log_stats(30.0, queue, udp_protocol, ws_server)))

    log.info("Orchestrator ready")


async def shutdown() -> None:
    """Tear down all subsystems cleanly."""
    global _simulator

    if csv_logger.active:
        csv_logger.stop()
    if playback_engine.active:
        playback_engine.stop()
    # A half-written track is safe to abandon: the completion flag is only
    # stamped at the end, so the next open recomputes it from row 0.
    pose_tracks.cancel_all()

    for task in _tasks:
        task.cancel()
    _tasks.clear()

    await osc_bridge.stop()
    await osc_live.stop()

    await bus.close()

    if _transport is not None:
        _transport.close()

    if _simulator is not None:
        await _simulator.stop()
        _simulator = None

    configurator.stop()
    log.info("Orchestrator shut down")
