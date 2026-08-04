// ── Playback ────────────────────────────────────────────────────────────────
// The progress bar is read-only by design: PlaybackEngine has no seek.
// Pause/resume state is always read back from the snapshot, never optimistic.

import {
  $, setText, setClass, setAttr, setDisabled, setHidden, syncControl, trackDirty,
  takeDuration, fmtCount,
} from "../dom.js";
import { on, state } from "../store.js";
import { api, action } from "../api.js";

let sessionTree = [];

export const isPlaying = () => !!(state.playback && state.playback.active);
export const isPaused = () => !!(state.playback && state.playback.paused);

// ── Session/take browser (on demand, GET /api/sessions) ─────────────────────

export async function refreshSessions() {
  try {
    const { sessions } = await api("GET", "/api/sessions");
    sessionTree = sessions;

    const sel = $("playback-session");
    const prev = sel.value;
    sel.innerHTML = "";
    for (const s of sessions) {
      const o = document.createElement("option");
      o.value = s.name;
      o.textContent = `${s.title || s.name} (${s.takes.length} takes)`;
      sel.append(o);
    }
    if (sessions.some((s) => s.name === prev)) sel.value = prev;
    if (!sessions.length) {
      const o = document.createElement("option");
      o.textContent = "(aucune session)";
      o.disabled = true;
      sel.append(o);
    }
    populateTakeSelect();
  } catch { /* the panel stays usable without the browser */ }
}

function populateTakeSelect() {
  const session = sessionTree.find((s) => s.name === $("playback-session").value);
  const sel = $("playback-take");
  const prev = sel.value;
  sel.innerHTML = "";

  const takes = session ? session.takes : [];
  for (const t of takes) {
    const o = document.createElement("option");
    o.value = t.name;
    o.textContent = `${t.name} — ${takeDuration(t)} s, ${fmtCount(t.packet_count)} paq.`;
    sel.append(o);
  }
  if (takes.some((t) => t.name === prev)) sel.value = prev;
  if (!takes.length) {
    const o = document.createElement("option");
    o.textContent = "(aucun take)";
    o.disabled = true;
    sel.append(o);
  }
  updateTakeMeta();
}

function updateTakeMeta() {
  const session = sessionTree.find((s) => s.name === $("playback-session").value);
  const t = session && session.takes.find((x) => x.name === $("playback-take").value);
  if (!t) { setText($("take-meta"), ""); return; }

  setText($("take-meta"), [
    t.title,
    t.performer && `perf : ${t.performer}`,
    t.figures && t.figures.length && `figures : ${t.figures.join(", ")}`,
    `${takeDuration(t)} s · ${fmtCount(t.packet_count)} paquets`,
    t.sync_marker_ts_us > 0 && "marqueur ✓",
  ].filter(Boolean).join(" · "));
}

// ── Render ──────────────────────────────────────────────────────────────────

function render(p) {
  if (!p) return;

  const badge = $("play-badge");
  setText(badge, p.active ? (p.paused ? "En pause" : "▶ Lecture") : "Inactif");
  setClass(badge, "badge" + (p.active ? (p.paused ? " badge--warn" : " badge--info") : ""));

  const pct = p.active ? p.percent : 0;
  const bar = $("playback-bar");
  if (bar.style.width !== pct + "%") bar.style.width = pct + "%";
  setClass(bar, "progress__bar" + (p.paused ? " paused" : ""));
  setAttr($("playback-progress"), "aria-valuenow", Math.round(pct));

  setText($("playback-elapsed"), `${p.elapsed_s} s`);
  setText($("playback-total"), `${p.total_s} s`);

  setDisabled($("playback-start"), p.active);
  setDisabled($("playback-pause"), !p.active);
  setDisabled($("playback-stop"), !p.active);

  const pause = $("playback-pause");
  setText(pause, p.paused ? "Reprendre" : "Pause");
  setAttr(pause, "aria-pressed", p.paused ? "true" : "false");

  syncControl($("playback-loop"), p.loop);

  renderSceneTransport(p, pct);
}

/**
 * The Scène tab's transport. Shown only while a replay is running: the engine
 * has no "loaded but not started" state — `start` takes a session and a take
 * and begins — so there is nothing to control before then, and picking a take
 * stays in Captation rather than being duplicated here.
 */
function renderSceneTransport(p, pct) {
  setHidden($("scene-transport"), !p.active);
  if (!p.active) return;

  setText($("scene-play-take"), `${p.session} / ${p.take}  ×${p.speed}${p.loop ? "  ⟳" : ""}`);

  const badge = $("scene-play-badge");
  setText(badge, p.paused ? "En pause" : "▶ Lecture");
  setClass(badge, "badge " + (p.paused ? "badge--warn" : "badge--info"));

  const bar = $("scene-play-bar");
  if (bar.style.width !== pct + "%") bar.style.width = pct + "%";
  setClass(bar, "progress__bar" + (p.paused ? " paused" : ""));
  setAttr($("scene-play-progress"), "aria-valuenow", Math.round(pct));

  setText($("scene-play-elapsed"), `${p.elapsed_s} s`);
  setText($("scene-play-total"), `${p.total_s} s`);

  const pause = $("scene-play-pause");
  setText(pause, p.paused ? "Reprendre" : "Pause");
  setAttr(pause, "aria-pressed", p.paused ? "true" : "false");
}

export function initPlayback() {
  trackDirty($("playback-speed"), $("playback-loop"));

  on("playback", render);

  // Live packets dropped at the socket while a replay is running
  // (core.accept_live). Invisible until now, and it explains the muted stream.
  on("status", (s) => {
    if (!s) return;
    const muted = s.udp.muted;
    setText($("playback-muted"),
      muted > 0 ? `${fmtCount(muted)} paquets live ignorés` : "");
  });

  // ── Commands ──────────────────────────────────────────────────────────────
  $("session-refresh").onclick = refreshSessions;
  $("playback-session").onchange = populateTakeSelect;
  $("playback-take").onchange = updateTakeMeta;

  for (const b of document.querySelectorAll(".speed-preset")) {
    b.onclick = () => {
      $("playback-speed").value = b.dataset.speed;
      $("playback-speed").dataset.dirty = "1";
    };
  }

  $("playback-start").onclick = action(() => api("POST", "/api/playback/start", {
    session: $("playback-session").value,
    take: $("playback-take").value,
    speed: parseFloat($("playback-speed").value),
    loop: $("playback-loop").checked,
  }), "Lecture démarrée");

  const pause = action(() =>
    api("POST", isPaused() ? "/api/playback/resume" : "/api/playback/pause"));
  const stop = action(() => api("POST", "/api/playback/stop"), "Lecture arrêtée");

  $("playback-pause").onclick = pause;
  $("playback-stop").onclick = stop;

  // Same two commands from the Scène transport.
  $("scene-play-pause").onclick = pause;
  $("scene-play-stop").onclick = stop;
}

/** Used by the keyboard shortcuts. */
export function togglePlayback() {
  if (isPlaying()) $("playback-pause").click();
  else $("playback-start").click();
}
export function stopPlayback() {
  if (!$("playback-stop").disabled) $("playback-stop").click();
}
export function toggleLoop() {
  const el = $("playback-loop");
  el.checked = !el.checked;
  el.dataset.dirty = "1";
}
