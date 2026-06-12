"""
transport/live_monitor.py — In-process observation of the live packet stream.

The processing_loop (core.py) is the single packet consumer; it feeds every
packet to observe().  The monitor keeps, per packet-type name:
  - a sliding 1 s window of arrival timestamps → rate in Hz
  - the latest payload values
plus the wall-clock age of the most recent packet (connection liveness).

snapshot() returns a JSON-friendly dict for GET /api/live, so the control panel
can render live metrics by polling — it never needs to know the wire protocol.

All access is from the event-loop thread (observe from processing_loop,
snapshot from a route handler), so no locking is needed.
"""

import time
from collections import defaultdict, deque

# Packet keys that are metadata, not sensor payload — excluded from `latest`.
_META_KEYS = frozenset(
    ("version", "type", "typeId", "seq", "ts_esp_us", "ts_rx_us", "dep_slots")
)


class LiveMonitor:
    """Tracks per-type packet rates, latest values, and connection liveness."""

    def __init__(self, window_s: float = 1.0, stale_after_s: float = 1.0):
        self._window       = window_s
        self._stale_after  = stale_after_s
        self._events: dict[str, deque[float]] = defaultdict(deque)  # arrival ts/type
        self._latest: dict[str, dict]         = {}                  # payload/type
        self._last_rx: float | None           = None               # monotonic

    def observe(self, packet: dict) -> None:
        """Record one packet. Called for raw packets and for computed output."""
        now  = time.monotonic()
        name = packet.get("type", "?")

        dq = self._events[name]
        dq.append(now)
        cutoff = now - self._window
        while dq and dq[0] < cutoff:
            dq.popleft()

        self._latest[name] = {k: v for k, v in packet.items() if k not in _META_KEYS}
        self._last_rx = now

    def snapshot(self) -> dict:
        """Return a JSON-serialisable view of the current live state."""
        now = time.monotonic()
        cutoff = now - self._window
        rates: dict[str, float] = {}
        for name, dq in self._events.items():
            while dq and dq[0] < cutoff:
                dq.popleft()
            if dq:
                rates[name] = round(len(dq) / self._window, 1)

        age_ms    = None if self._last_rx is None else round((now - self._last_rx) * 1000)
        connected = age_ms is not None and age_ms < self._stale_after * 1000

        battery  = self._latest.get("battery") or {}
        computed = self._latest.get("computed")
        torus    = None
        if computed and "px" in computed:
            torus = {"px": computed["px"], "py": computed["py"], "pz": computed["pz"]}

        return {
            "connected":   connected,
            "age_ms":      age_ms,
            "rates":       dict(sorted(rates.items())),
            "latest":      self._latest,
            "battery_pct": battery.get("percent"),
            "torus":       torus,
        }
