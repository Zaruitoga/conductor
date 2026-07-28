// ── Take recording ──────────────────────────────────────────────────────────
// A take requires an open session (routes return 409 otherwise), so the
// controls follow the session state.

import { $, h, setText, setClass, setHidden, setDisabled, fmtCount } from "../dom.js";
import { on, state } from "../store.js";
import { api, action } from "../api.js";
import { refreshSessions } from "./playback.js";

export const isRecording = () => !!(state.recording && state.recording.active);

function renderToggle(active) {
  const btn = $("rec-toggle");
  setClass(btn, "btn btn--lg " + (active ? "btn--rec" : "btn--primary"));
  btn.textContent = "";
  if (active) btn.append(h("span.rec-dot"), "Arrêter");
  else btn.append("Démarrer");
}

let lastActive = null;

export function initRecording() {
  on("recording", (r) => {
    if (!r) return;

    const badge = $("rec-badge");
    setText(badge, r.active ? "● REC" : "Inactif");
    setClass(badge, "badge" + (r.active ? " badge--bad" : ""));

    setText($("rec-take"), r.take || "—");
    setText($("rec-count"), fmtCount(r.packet_count));
    setDisabled($("rec-marker"), !r.active);

    if (r.active !== lastActive) {
      lastActive = r.active;
      renderToggle(r.active);
    }
  });

  on("session", (sess) => {
    const active = !!sess;
    setHidden($("rec-no-session"), active);
    setHidden($("rec-controls"), !active);
  });

  // ── Commands ──────────────────────────────────────────────────────────────
  $("rec-toggle").onclick = action(async () => {
    if (isRecording()) {
      const r = await api("POST", "/api/recording/stop");
      $("take-title").value = "";   // the next take gets its auto title
      await refreshSessions();
      return r;
    }
    return api("POST", "/api/recording/start", {
      title: $("take-title").value.trim(),
      performer: $("take-performer").value.trim(),
      figures: $("take-figures").value.split(",").map((f) => f.trim()).filter(Boolean),
      notes: $("take-notes").value,
    });
  });

  $("rec-marker").onclick = action(
    () => api("POST", "/api/recording/marker"), "Marqueur posé");
}

/** Used by the keyboard shortcuts. */
export const toggleRecording = () => $("rec-toggle").click();
export const putMarker = () => {
  if (!$("rec-marker").disabled) $("rec-marker").click();
};
