"""config.py — Global orchestrator settings."""

import os

# Network
UDP_HOST = "0.0.0.0"
UDP_PORT = 4210          # ESP32 sends sensor data here

WS_HOST  = "0.0.0.0"
WS_PORT  = 8081          # downstream clients (Three.js, Ableton…) connect here

API_HOST = "0.0.0.0"
API_PORT = 8000          # FastAPI control panel + REST API

# The ESP32 advertises this mDNS hostname; it is resolved to an IP at startup
# (EspConfigurator.resolve), so the ESP's DHCP address no longer needs to be
# hardcoded. A literal IPv4 here (e.g. "10.0.0.42") is used as-is, bypassing mDNS.
ESP_HOST    = "imu-cyrwheel.local"
CONFIG_PORT = 4211             # config port: PC → ESP commands (remote side)

# Local port we bind to receive CFG_ACK replies. Normally the same as
# CONFIG_PORT (the ESP answers to the port it was addressed from), but kept
# separate so a simulator can listen on its own port on this very machine.
CONFIG_LOCAL_PORT = 4211

# How long to wait for a CFG_ACK after a command. The firmware now yields to its
# config poll every ~50 ms (DRAIN_BUDGET_MS), so commands are acked in <100 ms
# (measured ~54 ms). 1 s leaves ample margin for an occasional flash write
# (saveNVS on set_simple/set_super) or a WiFi hiccup.
CONFIG_ACK_TIMEOUT_S = 1.0

# ESP health monitoring
HEARTBEAT_TIMEOUT_S = 6.0   # no heartbeat for this long ⇒ ESP considered offline
RATE_TOLERANCE      = 0.25  # a stream is "conform" if measured Hz ≥ expected × (1 − this)

# OSC bridge: bus → Ableton Live (AbletonOSC remote script). Retargetable at
# runtime via PATCH /api/osc/settings — these are only the boot defaults.
OSC_HOST        = "127.0.0.1"   # where AbletonOSC listens for commands
OSC_SEND_PORT   = 11000         # AbletonOSC's command port
OSC_LISTEN_PORT = 11001         # AbletonOSC's reply port — we listen here
OSC_RATE_HZ     = 30.0          # default cap on continuous-route sends

# Torus geometry — the single source, read by model/signals/wheel.py,
# GET /api/config and simulator/motion.py alike. These are *measurements* of
# the wheel, not settings: they were deliberately taken off the parameter
# surface (ADR 0004), because a pose track is precomputed from them and moving
# either mid-séance would silently invalidate every track already on disk.
# Swapping wheels means editing here and restarting.
R_TORE = 1.0             # major radius (metres)
r_TORE = 0.05            # tube radius (metres)

# Pipeline
DEGENERATE_THRESHOLD = 1e-6   # u_perp below which the wheel is considered flat

# Largest gap between two consecutive ts_esp_us that is still treated as real
# elapsed time by TorusPositionStage. Above it the value is a time-base
# discontinuity (ESP reboot, long dropout, or the packet straddling the
# live↔replay switch), not motion: integrating it would jump px/py by metres.
# 0.5 s ≈ 50 missed packets at 100 Hz — a genuine gap that long is not worth
# integrating blind anyway.
MAX_DT_S = 0.5

# ── Simulator (development) ──────────────────────────────────────────────────
# The `simulator` package impersonates the ESP32 over real UDP sockets, so the
# whole chain (parsing, layout, health, pipeline, CSV, WS) can be exercised
# without hardware. Selected with the SIM environment variable:
#
#   SIM=1 | SIM=embed   talk to the local fake ESP *and* run it in-process
#   SIM=extern          talk to the local fake ESP, started in another terminal
#                       (python3 -m simulator)
#   unset               production: real hardware, nothing below applies
#
# The simulator listens on its own config port because the orchestrator already
# owns CONFIG_LOCAL_PORT (4211) on this machine.
SIM_CONFIG_PORT = 4311
_SIM_MODE       = os.getenv("SIM", "")
SIM_ENABLED     = _SIM_MODE in ("1", "embed", "extern")
SIM_EMBEDDED    = _SIM_MODE in ("1", "embed")
SIM_SCENARIO    = os.getenv("SIM_SCENARIO", "coin")   # see simulator/motion.py

if SIM_ENABLED:
    ESP_HOST    = "127.0.0.1"
    CONFIG_PORT = SIM_CONFIG_PORT
