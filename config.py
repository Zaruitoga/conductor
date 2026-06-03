"""config.py — Global orchestrator settings."""

# Network
UDP_HOST = "0.0.0.0"
UDP_PORT = 4210          # ESP32 sends sensor data here

WS_HOST  = "0.0.0.0"
WS_PORT  = 8081          # downstream clients (Three.js, Ableton…) connect here

ESP_IP      = "10.89.55.66"   # ESP32 IP address on the local network
CONFIG_PORT = 4211             # config port: PC → ESP commands and ACK replies

# Torus geometry
R_TORE = 1.0             # major radius (metres)
r_TORE = 0.05            # tube radius (metres)

# Pipeline
DEGENERATE_THRESHOLD = 1e-6   # u_perp below which the wheel is considered flat
