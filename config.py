"""config.py — Global orchestrator settings."""

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
CONFIG_PORT = 4211             # config port: PC → ESP commands and ACK replies

# ESP health monitoring
HEARTBEAT_TIMEOUT_S = 6.0   # no heartbeat for this long ⇒ ESP considered offline
RATE_TOLERANCE      = 0.25  # a stream is "conform" if measured Hz ≥ expected × (1 − this)

# Torus geometry
R_TORE = 1.0             # major radius (metres)
r_TORE = 0.05            # tube radius (metres)

# Pipeline
DEGENERATE_THRESHOLD = 1e-6   # u_perp below which the wheel is considered flat
