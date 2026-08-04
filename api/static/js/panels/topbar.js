// ── Topbar: connection verdict, mode, and alerts ────────────────────────────
// This is the only place `health.state` is rendered — it must be true on every
// tab, so it lives above them.
//
// The counters are deliberately quiet. `err` and `file` are hidden while they
// are zero: a permanent "err 0" trains you to stop reading the line, which
// defeats the point of having it. A growing queue is the exact failure mode the
// WS fan-out seam exists to prevent, so it is worth showing the moment it moves.

import { $, setText, setClass, setHidden, fmtCount } from "../dom.js";
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

    setText($("m-err"), fmtCount(s.udp.errors));
    setHidden($("m-err-box"), !s.udp.errors);

    setText($("m-queue"), s.queue_depth);
    setHidden($("m-queue-box"), !s.queue_depth);

    setText($("m-ws"), s.ws.clients);
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
