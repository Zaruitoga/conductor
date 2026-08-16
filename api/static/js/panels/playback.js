// ── Playback ────────────────────────────────────────────────────────────────
// Pause/resume state is always read back from the snapshot, never optimistic.
//
// The progress bar is a *seek* control (both of them: Captation and Scène).
// Dragging it resumes the replay at the instant aimed at, which goes through
// the jump — a warm-up and a position seeded from the pose track, so the wheel
// does not teleport (storage/seek.py). What it deliberately is not is a
// *sweep*: parcourir un take au curseur means watching the wheel and the video
// follow it, and this panel has neither on screen. That lives in the viz, which
// does (api/viz/sweep.js, ADR 0004).

import {
  $, setText, setClass, setAttr, setDisabled, setHidden, syncControl, trackDirty,
  takeDuration, fmtCount,
} from "../dom.js";
import { on, state } from "../store.js";
import { api, action, toast } from "../api.js";

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
  ].filter(Boolean).join(" · "));
}

// ── Render ──────────────────────────────────────────────────────────────────

function render(p) {
  if (!p) return;

  const badge = $("play-badge");
  setText(badge, p.active ? (p.paused ? "En pause" : "▶ Lecture") : "Inactif");
  setClass(badge, "badge" + (p.active ? (p.paused ? " badge--warn" : " badge--info") : ""));

  // A jump costs a warm-up and this snapshot is 4 Hz, so `elapsed_s` still names
  // the place we came from for a moment after the bar was let go. Showing it
  // would snap the bar back and then jump; the target is held until the replay
  // reaches it (see `settleSeek`).
  settleSeek(p);
  const shown = seekTo !== null ? seekTo : (p.active ? p.elapsed_s : 0);
  const pct = p.active && p.total_s > 0
    ? Math.max(0, Math.min(100, (shown / p.total_s) * 100))
    : 0;

  renderBar("playback-progress", "playback-bar", "playback-head", pct, shown, p);
  setText($("playback-elapsed"), `${shown.toFixed(1)} s`);
  setText($("playback-total"), `${p.total_s} s`);

  setDisabled($("playback-start"), p.active);
  setDisabled($("playback-pause"), !p.active);
  setDisabled($("playback-stop"), !p.active);

  const pause = $("playback-pause");
  setText(pause, p.paused ? "Reprendre" : "Pause");
  setAttr(pause, "aria-pressed", p.paused ? "true" : "false");

  syncControl($("playback-loop"), p.loop);

  renderSceneTransport(p, pct, shown);
}

/**
 * One bar: the fill, the handle, and what a screen reader reads.
 *
 * `aria-valuenow` is in *take seconds*, not per cent — the value being chosen
 * is an instant of the take, and "34.3" beside "58.9" is what says where that
 * is; a percentage would have to be converted by whoever is listening.
 */
function renderBar(barId, fillId, headId, pct, shown, p) {
  const fill = $(fillId);
  if (fill.style.width !== pct + "%") fill.style.width = pct + "%";
  setClass(fill, "progress__bar" + (p.paused ? " paused" : ""));
  const head = $(headId);
  if (head.style.left !== pct + "%") head.style.left = pct + "%";

  const el = $(barId);
  setClass(el, "progress progress--seek" + (seekTo !== null ? " seeking" : ""));
  setAttr(el, "aria-valuemax", String(p.total_s || 0));
  setAttr(el, "aria-valuenow", shown.toFixed(1));
  setAttr(el, "aria-valuetext", `${shown.toFixed(1)} s`);
  // Nothing to aim at until a replay is running: the engine has no "loaded but
  // not started" state, so before then the bar is a témoin and says so.
  setAttr(el, "aria-disabled", p.active ? "false" : "true");
}

/**
 * The Scène tab's transport. Shown only while a replay is running: the engine
 * has no "loaded but not started" state — `start` takes a session and a take
 * and begins — so there is nothing to control before then, and picking a take
 * stays in Captation rather than being duplicated here.
 */
function renderSceneTransport(p, pct, shown) {
  setHidden($("scene-transport"), !p.active);
  if (!p.active) return;

  setText($("scene-play-take"), `${p.session} / ${p.take}  ×${p.speed}${p.loop ? "  ⟳" : ""}`);

  const badge = $("scene-play-badge");
  setText(badge, p.paused ? "En pause" : "▶ Lecture");
  setClass(badge, "badge " + (p.paused ? "badge--warn" : "badge--info"));

  renderBar("scene-play-progress", "scene-play-bar", "scene-play-head", pct, shown, p);

  setText($("scene-play-elapsed"), `${shown.toFixed(1)} s`);
  setText($("scene-play-total"), `${p.total_s} s`);

  const pause = $("scene-play-pause");
  setText(pause, p.paused ? "Reprendre" : "Pause");
  setAttr(pause, "aria-pressed", p.paused ? "true" : "false");
}

// ── Seeking: the bar as a control ───────────────────────────────────────────
//
// Both bars share this state because they show the same replay — grabbing one
// while the other is on screen is not a case, they are in different tabs.
let seekTo   = null;   // the instant chosen, until the replay reaches it
let seekAt   = 0;      // wall clock: how long we are prepared to hold it
let dragging = false;
let keyCommit = null;

/** Let go of the held target once the replay has arrived — or plainly won't. */
function settleSeek(p) {
  if (seekTo === null || dragging) return;
  const arrived = p.active && Math.abs(p.elapsed_s - seekTo) < 1.5;
  if (!p.active || arrived || Date.now() - seekAt > 4000) seekTo = null;
}

function timeAt(el, clientX) {
  const total = (state.playback && state.playback.total_s) || 0;
  const r = el.getBoundingClientRect();
  const frac = r.width > 0 ? (clientX - r.left) / r.width : 0;
  return Math.max(0, Math.min(1, frac)) * total;
}

/** Where the bar is aiming right now — what a key press moves from. */
function aimedAt() {
  if (seekTo !== null) return seekTo;
  return (state.playback && state.playback.elapsed_s) || 0;
}

function aim(t) {
  const total = (state.playback && state.playback.total_s) || 0;
  seekTo = Math.max(0, Math.min(total, t));
  seekAt = Date.now();
  render(state.playback);
}

/**
 * Commit the instant aimed at.
 *
 * Not wrapped in `action()`, deliberately: this is the one command that can be
 * issued several times in a gesture, and a toast per drag would bury the ones
 * that mean something. A refusal still shows — and drops the held target, so
 * the bar goes back to telling the truth rather than pointing where the replay
 * never went.
 */
async function commitSeek() {
  const t = seekTo;
  if (t === null) return;
  try {
    await api("POST", "/api/playback/seek", { t });
  } catch (e) {
    seekTo = null;
    toast(e.message, "bad");
  }
}

function initSeekBar(barId) {
  const el = $(barId);

  el.addEventListener("pointerdown", (e) => {
    if (!isPlaying()) return;
    e.preventDefault();
    el.setPointerCapture(e.pointerId);
    el.focus();
    dragging = true;
    aim(timeAt(el, e.clientX));
  });
  el.addEventListener("pointermove", (e) => {
    if (dragging) aim(timeAt(el, e.clientX));
  });
  const release = () => {
    if (!dragging) return;
    dragging = false;
    if (seekTo !== null) commitSeek();
  };
  el.addEventListener("pointerup", release);
  el.addEventListener("pointercancel", release);

  // The same gesture without a mouse. The commit is trailing: a key held down
  // produces a stream of moves, and the engine is being told about the last.
  el.addEventListener("keydown", (e) => {
    if (!isPlaying()) return;
    const total = (state.playback && state.playback.total_s) || 0;
    const step = { ArrowLeft: -1, ArrowRight: 1, PageDown: -10, PageUp: 10 }[e.key];
    let target = null;
    if (step !== undefined) target = aimedAt() + step * (e.shiftKey ? 0.1 : 1);
    else if (e.key === "Home") target = 0;
    else if (e.key === "End") target = total;
    else return;
    e.preventDefault();
    aim(target);
    clearTimeout(keyCommit);
    keyCommit = setTimeout(() => { if (seekTo !== null) commitSeek(); }, 300);
  });
}

export function initPlayback() {
  trackDirty($("playback-speed"), $("playback-loop"));

  on("playback", render);
  initSeekBar("playback-progress");
  initSeekBar("scene-play-progress");

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
