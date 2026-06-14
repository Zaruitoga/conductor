"""
transport/esp_health.py — Unified ESP32 health & connection monitor.

Single source of truth for "is the ESP alive and behaving?".  It fuses two
signals so the panel shows one verdict instead of several redundant indicators:

  1. Presence + telemetry — from the periodic heartbeat packet (type 0x20):
     uptime, packets sent, UDP errors, WiFi RSSI, CPU temperature, battery.
     No heartbeat for HEARTBEAT_TIMEOUT_S ⇒ the ESP is considered offline,
     independently of whether any sensor stream is running.

  2. Stream conformance — cross-checks the *measured* per-type rates
     (LiveMonitor) against what the *configured* ESP state (the last CFG_ACK,
     EspConfigurator.state) says we should be receiving.  A configured stream
     that is absent or too slow flags the ESP as degraded.

snapshot() returns one JSON-friendly dict consumed by the panel; see its
docstring for the shape.  All access is from the event-loop thread.
"""

from transport.udp_receiver import TYPE_NAME

# Types that are never "expected sensor streams": telemetry and pipeline output.
_IGNORED_STREAMS = frozenset({"heartbeat", "computed"})


class EspHealth:
    """Synthesises a single health verdict from heartbeat + stream conformance."""

    def __init__(self, monitor, configurator,
                 heartbeat_timeout_s: float = 6.0,
                 rate_tolerance: float = 0.25):
        self._monitor       = monitor
        self._configurator  = configurator
        self._timeout_s     = heartbeat_timeout_s
        self._tol           = rate_tolerance

    # ── Public API ───────────────────────────────────────────────────────────

    def snapshot(self) -> dict:
        """
        Return the unified health view:

          {
            "state":   "online" | "degraded" | "offline",
            "reason":  "<short phrase>",
            "heartbeat": { age_ms, online, uptime_ms, packets_sent,
                           udp_errors, rssi_dbm, cpu_temp_c, battery_pct } | None,
            "streams": [ { type, expected_hz | None, actual_hz, status } ],
          }

        status ∈ {ok, slow, missing, unexpected}.
        """
        live      = self._monitor.snapshot()
        rates     = live.get("rates", {})
        hb_age_ms = live.get("heartbeat_age_ms")
        latest_hb = (live.get("latest", {}) or {}).get("heartbeat") or {}

        heartbeat, online = self._heartbeat_block(hb_age_ms, latest_hb)
        streams           = self._streams(rates)
        state, reason     = self._assess(hb_age_ms, online, streams)

        return {
            "state":     state,
            "reason":    reason,
            "heartbeat": heartbeat,
            "streams":   streams,
        }

    # ── Heartbeat ────────────────────────────────────────────────────────────

    def _heartbeat_block(self, hb_age_ms, latest_hb):
        if hb_age_ms is None:
            return None, False
        online  = hb_age_ms < self._timeout_s * 1000
        battery = latest_hb.get("battery_pct")
        block = {
            "age_ms":       hb_age_ms,
            "online":       online,
            "uptime_ms":    latest_hb.get("uptime_ms"),
            "packets_sent": latest_hb.get("packets_sent"),
            "udp_errors":   latest_hb.get("udp_errors"),
            "rssi_dbm":     latest_hb.get("rssi_dbm"),
            "cpu_temp_c":   latest_hb.get("cpu_temp_c"),
            # battery_pct is -1 when the ESP has no fuel gauge → expose as None
            "battery_pct":  battery if (battery is not None and battery >= 0) else None,
        }
        return block, online

    # ── Stream conformance ───────────────────────────────────────────────────

    def _expected_streams(self) -> dict[str, float | None]:
        """Map of expected stream name → expected Hz (None = presence-only)."""
        state = self._configurator.state
        if not state:
            return {}

        expected: dict[str, float | None] = {}
        simple_rate_by_slot: dict[int, float] = {}

        for s in state.get("simples", []):
            if not s.get("enabled"):
                continue
            try:
                name = TYPE_NAME[int(s["pkt_type"], 16)]
            except (KeyError, ValueError, TypeError):
                continue
            rate = s.get("rate_hz") or 0
            expected[name] = rate or None
            simple_rate_by_slot[s["slot"]] = rate

        for sup in state.get("supers", []):
            if not sup.get("active"):
                continue
            name = f"super_{sup['slot']}"
            deps = sup.get("deps", []) or []
            skip = sup.get("skip_ratio") or 1
            dep_rates = [simple_rate_by_slot.get(d, 0) for d in deps]
            if deps and all(r > 0 for r in dep_rates):
                # Super fires at the slowest dep's rate, decimated by skip_ratio.
                expected[name] = round(min(dep_rates) / skip, 1)
            else:
                expected[name] = None   # presence-only check
        return expected

    def _streams(self, rates: dict[str, float]) -> list[dict]:
        expected = self._expected_streams()
        observed = {t for t in rates if t not in _IGNORED_STREAMS}

        streams = []
        for name in sorted(set(expected) | observed):
            exp = expected.get(name)
            act = round(rates.get(name, 0.0), 1)
            streams.append({
                "type":        name,
                "expected_hz": exp,
                "actual_hz":   act,
                "status":      self._status(name in expected, exp, act),
            })
        return streams

    def _status(self, is_expected: bool, expected_hz, actual_hz: float) -> str:
        if not is_expected:
            return "unexpected"
        if actual_hz <= 0:
            return "missing"
        if expected_hz is None:
            return "ok"   # presence-only and present
        if actual_hz < expected_hz * (1 - self._tol):
            return "slow"
        return "ok"

    # ── Overall verdict ──────────────────────────────────────────────────────

    def _assess(self, hb_age_ms, online: bool, streams: list[dict]):
        if hb_age_ms is None:
            return "offline", "aucun heartbeat reçu"
        if not online:
            return "offline", f"pas de heartbeat depuis {hb_age_ms // 1000} s"

        problem = next(
            (s for s in streams if s["status"] in ("missing", "slow")), None
        )
        if problem is not None:
            if problem["status"] == "missing":
                return "degraded", f"{problem['type']} absent"
            return "degraded", (
                f"{problem['type']} à {problem['actual_hz']}/"
                f"{problem['expected_hz']} Hz"
            )
        return "online", "ESP en ligne"
