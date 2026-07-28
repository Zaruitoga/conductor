"""
simulator — A fake ESP32 + IMU, for developing without the hardware.

Speaks the real binary protocol over real UDP sockets, so everything below the
socket in the orchestrator (parsing, super-slot layout, ESP health, pipeline,
CSV, WebSocket, control panel) runs unmodified and untricked.

Two ways to run it, selected by the SIM environment variable (see config.py):

    SIM=1 python3 main.py            in-process, one command
    python3 -m simulator             standalone, then SIM=extern python3 main.py

Standalone is the one to use when iterating on scenarios, since the simulator
can be restarted without bouncing the orchestrator.

This package is imported only when SIM is set; production never loads it.
"""

from simulator.esp32 import Esp32Simulator, start_simulator
from simulator.motion import SCENARIOS, WheelMotion

__all__ = ["Esp32Simulator", "start_simulator", "WheelMotion", "SCENARIOS"]
