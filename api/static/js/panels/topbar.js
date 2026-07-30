// ── Topbar: connection state, mode badge, transport counters ────────────────

import { $, setText, setClass, fmtCount } from "../dom.js";
import { on, setConnectionHandler } from "../store.js";

const STATE_DOT = { online: "ok", degraded: "warn", offline: "bad" };

export function initTopbar() {
  on("health", (health) => {
    if (!health) return;
    const state = health.state || "offline";
    setClass($("conn-dot"), "dot " + (STATE_DOT[state] || "bad"));
    setText($("conn-text"), health.reason || state);
  });

  on("status", (s) => {
    if (!s) return;
    setText($("mode-badge"), s.mode);
    setClass($("mode-badge"), "mode " + s.mode);
    setText($("m-rx"), fmtCount(s.udp.rx));
    setText($("m-err"), fmtCount(s.udp.errors));
    setText($("m-queue"), s.queue_depth);
    setText($("m-ws"), s.ws.clients);
  });

  // A glance at whether Live is actually being fed, next to the transport
  // counters — the same reasoning as m-ws, one level downstream.
  on("osc", (o) => {
    if (!o) return;
    setText($("m-osc"), o.enabled ? o.out_hz : "off");
  });

  // While the WS is down we keep rendering, but if the REST fallback also
  // fails the panel must say so rather than show stale numbers.
  setConnectionHandler((connected, error) => {
    if (error) {
      setClass($("conn-dot"), "dot bad");
      setText($("conn-text"), error);
    }
  });
}
