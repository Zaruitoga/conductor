"""
main.py — Cyr IMU orchestrator, iteration 3.

Architecture:

  [LIVE]     UDPReceiver ──────────────────────┐
                                               ▼
                                            Queue ──▶ Pipeline ──▶ WSServer
                                               ▲
  [PLAYBACK] PlaybackEngine (CSV) ─────────────┘

  EspConfigurator ──UDP cfg──▶ ESP32 (port 4211)
                  ◀──ACK─────
                  └──────────▶ SuperSlotLayout (shared)
                                    └──▶ UDPReceiver (named field decoding)

Super-slot field naming:
  On startup, get_state() fetches the ESP config and populates SuperSlotLayout.
  From that point, super-slot packets are decoded into named fields
  (gyro_x, game_rv_qw, …) instead of the opaque s{i} fallback.

Keyboard commands (temporary — to be replaced by a FastAPI layer in iteration 4):
  r              — start recording
  s              — stop recording
  m              — video sync marker (clap)
  p <name>       — play back a session  (e.g. p 2026-05-23_14-32-10)
  x              — stop playback
  l              — list available sessions
  q              — quit

  e get                           — display full ESP state
  e host [ip]                     — SET_HOST (auto-detected if no ip given)
  e s <slot> <on|off> <hz>        — configure a simple slot
  e super <slot> <d,d,...> [skip] — configure a super slot
  e del <slot>                    — delete a super slot
"""

import asyncio
import logging
import socket as _socket
import sys

import config
from transport.super_layout     import SuperSlotLayout
from transport.udp_receiver     import start_udp_receiver
from transport.esp_configurator import EspConfigurator
from transport.ws_server        import WSServer
from pipeline.torus_position    import TorusPositionStage
from storage.session_manager    import SessionManager
from storage.csv_logger         import CSVLogger
from storage.playback_engine    import PlaybackEngine, SENTINEL


def _local_ip() -> str:
    """Detect the active local IP by opening a dummy UDP connection."""
    s = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
    s.connect(("8.8.8.8", 80))
    ip = s.getsockname()[0]
    s.close()
    return ip


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("main")

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

configurator = EspConfigurator(
    esp_ip      = config.ESP_IP,
    config_port = config.CONFIG_PORT,
    local_port  = config.CONFIG_PORT,
    layout      = layout,
)


async def processing_loop(queue: asyncio.Queue, ws_server: WSServer) -> None:
    """
    Main packet consumer loop.

    For each packet dequeued:
      1. Write to CSV (raw, before pipeline transforms)
      2. Run through all pipeline stages (a None result drops the packet)
      3. Broadcast the enriched packet over WebSocket
    """
    log.info("Processing loop started")
    while True:
        packet = await queue.get()

        if packet.get("typeId") == "playback_end":
            log.info("Playback session ended — returning to IDLE")
            queue.task_done()
            continue

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
            await ws_server.broadcast(packet)

        queue.task_done()


HELP = """
Commands:
  r              — start recording
  s              — stop recording
  m              — video sync marker (clap)
  p <name>       — play back a session  (e.g. p 2026-05-23_14-32-10)
  x              — stop playback
  l              — list sessions
  q              — quit

  e get                           — full ESP state
  e host [ip]                     — SET_HOST (auto-detected if omitted)
  e s <slot> <on|off> <hz>        — simple slot  (e.g. e s 0 on 50)
  e super <slot> <d,d,...> [skip] — super slot   (e.g. e super 0 0,6)
  e del <slot>                    — delete a super slot
"""


async def keyboard_control(queue: asyncio.Queue) -> None:
    """
    Read commands from stdin without blocking the event loop.

    Temporary control interface — will be replaced by FastAPI endpoints
    in iteration 4.
    """
    loop   = asyncio.get_event_loop()
    reader = asyncio.StreamReader()
    await loop.connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), sys.stdin
    )
    print(HELP)

    while True:
        try:
            line = await reader.readline()
        except Exception:
            break
        cmd = line.decode().strip()
        if not cmd:
            continue

        if cmd == "r":
            if csv_logger.active:
                log.warning("Recording already active")
            elif playback_engine.active:
                log.warning("Cannot record during playback")
            else:
                session_dir, meta = session_manager.new_session()
                csv_logger.start(session_dir, meta)

        elif cmd == "s":
            if csv_logger.active:
                csv_logger.stop()
            else:
                log.warning("No active recording")

        elif cmd == "m":
            import time
            ts = time.time_ns() // 1000
            csv_logger.mark_sync(ts)

        elif cmd.startswith("p "):
            session_name = cmd[2:].strip()
            if csv_logger.active:
                log.warning("Stop recording before starting playback")
            elif playback_engine.active:
                log.warning("Playback already active — press x to stop")
            else:
                await playback_engine.start(
                    session_name, queue, PIPELINE_STAGES
                )

        elif cmd == "x":
            if playback_engine.active:
                playback_engine.stop()
            else:
                log.warning("No active playback")

        elif cmd == "l":
            sessions = session_manager.list_sessions()
            if sessions:
                print("\nAvailable sessions:")
                for s in sessions:
                    print(f"  {s}")
                print()
            else:
                print("  (no sessions recorded)")

        elif cmd == "q":
            if csv_logger.active:
                csv_logger.stop()
            if playback_engine.active:
                playback_engine.stop()
            log.info("Shutdown requested")
            asyncio.get_event_loop().stop()
            break

        elif cmd.startswith("e ") or cmd == "e":
            await _handle_esp_cmd(cmd[2:].strip())

        else:
            print(HELP)


async def _handle_esp_cmd(args: str) -> None:
    """
    Dispatch 'e …' sub-commands to EspConfigurator.

    Runs in a thread executor to avoid blocking the asyncio event loop.
    The layout is updated automatically inside EspConfigurator after each ACK.
    """
    loop  = asyncio.get_event_loop()
    parts = args.split()

    if not parts or parts[0] == "get":
        await loop.run_in_executor(None, configurator.get_state)

    elif parts[0] == "host":
        ip = parts[1] if len(parts) > 1 else _local_ip()
        await loop.run_in_executor(None, configurator.set_host, ip)

    elif parts[0] == "s" and len(parts) >= 4:
        try:
            slot    = int(parts[1])
            enabled = parts[2].lower() == "on"
            rate_us = int(1e6 / float(parts[3]))
            await loop.run_in_executor(
                None, configurator.set_simple, slot, enabled, rate_us
            )
        except (ValueError, ZeroDivisionError) as exc:
            log.error(f"Invalid syntax: {exc}")

    elif parts[0] == "super" and len(parts) >= 3:
        try:
            slot      = int(parts[1])
            dep_slots = [int(d) for d in parts[2].split(",")]
            skip      = int(parts[3]) if len(parts) > 3 else 1
            await loop.run_in_executor(
                None, configurator.set_super, slot, dep_slots, skip
            )
        except ValueError as exc:
            log.error(f"Invalid syntax: {exc}")

    elif parts[0] == "del" and len(parts) >= 2:
        try:
            slot = int(parts[1])
            await loop.run_in_executor(None, configurator.del_super, slot)
        except ValueError as exc:
            log.error(f"Invalid syntax: {exc}")

    else:
        print("Usage: e get | e host [ip] | e s <slot> <on|off> <hz> "
              "| e super <slot> <d,d,...> [skip] | e del <slot>")


async def log_stats(
    interval: float,
    queue: asyncio.Queue,
    udp_protocol,
    ws_server: WSServer,
) -> None:
    """Log a periodic status line with queue depth, packet counts, and client count."""
    while True:
        await asyncio.sleep(interval)
        mode = "REC" if csv_logger.active else ("PLAY" if playback_engine.active else "IDLE")
        log.info(
            f"[{mode}]  Queue:{queue.qsize()}  "
            f"UDP rx:{udp_protocol.stats['rx']} err:{udp_protocol.stats['errors']}  "
            f"WS tx:{ws_server.stats['tx']} clients:{len(ws_server.clients)}"
        )


async def main() -> None:
    """Initialise all subsystems and run the event loop indefinitely."""
    loop  = asyncio.get_event_loop()
    queue = asyncio.Queue()

    ws_server = WSServer(config.WS_HOST, config.WS_PORT)
    await ws_server.start()

    _transport, udp_protocol = await start_udp_receiver(
        config.UDP_HOST, config.UDP_PORT, queue, layout
    )

    configurator.start()
    my_ip = _local_ip()
    log.info(f"Local IP: {my_ip}")
    # SET_HOST also populates the layout via the ACK, so super packets are
    # decoded into named fields immediately after this call returns.
    await loop.run_in_executor(None, configurator.set_host, my_ip)

    asyncio.ensure_future(processing_loop(queue, ws_server))
    asyncio.ensure_future(log_stats(10.0, queue, udp_protocol, ws_server))
    asyncio.ensure_future(keyboard_control(queue))

    log.info("Orchestrator ready — press 'r' to record, 'q' to quit")
    await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        if csv_logger.active:
            csv_logger.stop()
        configurator.stop()
        log.info("Clean shutdown.")
