"""
simulator/__main__.py — Standalone entry point for the fake ESP32.

    python3 -m simulator --scenario coin

Then start the orchestrator pointed at it, in another terminal:

    SIM=extern python3 main.py

(`SIM=extern` makes config.py address 127.0.0.1 on the simulator's config port
without also starting a second simulator in-process.)
"""

import argparse
import asyncio
import logging

import config
from simulator.esp32 import Esp32Simulator
from simulator.motion import SCENARIOS, WheelMotion

log = logging.getLogger("simulator")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python3 -m simulator",
        description="Fake ESP32 + IMU streaming the real UDP protocol.",
    )
    p.add_argument("--scenario", choices=SCENARIOS, default=config.SIM_SCENARIO,
                   help="wheel motion to simulate (default: %(default)s)")
    p.add_argument("--config-port", type=int, default=config.SIM_CONFIG_PORT,
                   help="port to receive config commands on (default: %(default)s)")
    p.add_argument("--data-host", default="127.0.0.1",
                   help="where to send sensor data until SET_HOST says otherwise")
    p.add_argument("--data-port", type=int, default=config.UDP_PORT,
                   help="sensor data port (default: %(default)s)")
    p.add_argument("--rate", type=float, default=100.0,
                   help="boot sample rate in Hz for the enabled slots "
                        "(default: %(default)s)")
    p.add_argument("--heartbeat-hz", type=float, default=1.0,
                   help="heartbeat rate; 0 disables it (default: %(default)s)")
    p.add_argument("--lean", type=float, default=20.0,
                   help="lean angle in degrees, 0 = upright (default: %(default)s)")
    p.add_argument("--spin", type=float, default=180.0,
                   help="spin rate about the axle in deg/s (default: %(default)s)")
    p.add_argument("--precession", type=float, default=45.0,
                   help="precession rate in deg/s (default: %(default)s)")
    p.add_argument("--no-dep-simples", action="store_true",
                   help="do not emit standalone packets for slots that already "
                        "feed an active super slot")
    p.add_argument("--truth-interval", type=float, default=2.0,
                   help="seconds between ground-truth log lines; 0 disables "
                        "(default: %(default)s)")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return p.parse_args()


async def _run(args: argparse.Namespace) -> None:
    sim = Esp32Simulator(
        WheelMotion(
            scenario       = args.scenario,
            lean_deg       = args.lean,
            spin_dps       = args.spin,
            precession_dps = args.precession,
        ),
        config_port      = args.config_port,
        data_host        = args.data_host,
        data_port        = args.data_port,
        rate_hz          = args.rate,
        heartbeat_hz     = args.heartbeat_hz,
        emit_dep_simples = not args.no_dep_simples,
        truth_interval_s = args.truth_interval,
    )
    await sim.start()
    try:
        await asyncio.Event().wait()      # run until interrupted
    finally:
        await sim.stop()


if __name__ == "__main__":
    args = _parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        pass
