// ── Snapshot store ──────────────────────────────────────────────────────────
// Single ingestion point for the observation snapshot. The backend pushes the
// whole thing over /api/ws at ~4 Hz (core.panel_snapshot); if the socket drops
// we fall back to REST polling of the same per-section dicts.
//
// Panels subscribe per section. Two sections — `session` and `esp` — drive form
// rebuilds and are change-gated, so the 4 Hz push never rebuilds a form under
// the user's cursor. Everything else re-renders every tick, which is safe
// because the render helpers (setText/keyed/syncControl) write idempotently.

import { api } from "./api.js";

export const SECTIONS = [
  "status", "live", "health", "session", "recording", "playback", "esp", "model", "osc",
];

/** Sections whose renderers rebuild forms — only emitted when they change. */
const GATED = new Set(["session", "esp"]);

export const state = Object.fromEntries(SECTIONS.map((k) => [k, null]));

const listeners = Object.fromEntries(SECTIONS.map((k) => [k, []]));
const lastJson = {};

/** Subscribe to a section. Fires immediately if we already have data. */
export function on(section, fn) {
  listeners[section].push(fn);
  if (state[section] !== null) fn(state[section]);
}

function emit(section, value) {
  for (const fn of listeners[section]) {
    try { fn(value); } catch (e) { console.error(`[${section}]`, e); }
  }
}

/** Feed one section. Returns true if listeners were notified. */
export function ingestSection(section, value) {
  state[section] = value;
  if (GATED.has(section)) {
    const json = JSON.stringify(value ?? null);
    if (json === lastJson[section]) return false;
    lastJson[section] = json;
  }
  emit(section, value);
  return true;
}

/** Feed a full snapshot from the WS. */
export function ingest(snapshot) {
  feedRateHistory(snapshot.live);
  for (const key of SECTIONS) {
    if (key in snapshot) ingestSection(key, snapshot[key]);
  }
}

// ── Rate history (sparklines) ───────────────────────────────────────────────
// The backend only sends instantaneous rates, so the history lives here.
// 120 samples at 4 Hz ≈ 30 s.

export const HISTORY_LEN = 120;
export const rateHistory = new Map();

function feedRateHistory(live) {
  if (!live) return;
  const rates = live.rates || {};
  // Iterate the union so a stream that stops still gets zeroes pushed and
  // its sparkline visibly falls off, instead of freezing at its last value.
  const keys = new Set([...rateHistory.keys(), ...Object.keys(rates)]);
  for (const k of keys) {
    let buf = rateHistory.get(k);
    if (!buf) { buf = []; rateHistory.set(k, buf); }
    buf.push(rates[k] ?? 0);
    if (buf.length > HISTORY_LEN) buf.shift();
  }
}

export const historyOf = (type) => rateHistory.get(type) || [];

// ── Connection ──────────────────────────────────────────────────────────────

let onConnectionChange = () => {};
export function setConnectionHandler(fn) { onConnectionChange = fn; }

// REST fallback, only while the socket is down. Two tiers, as before:
// fast-moving sections at 400 ms, the rest at 1 s. There is deliberately no
// GET /api/esp/state, so `esp` simply holds its last pushed value.
let fallbackTimers = [];

async function poll(section, path, pick) {
  try {
    const data = await api("GET", path);
    ingestSection(section, pick ? pick(data) : data);
    return true;
  } catch {
    return false;
  }
}

const pollFast = () => Promise.all([
  poll("live", "/api/live"),
  poll("health", "/api/health"),
  poll("playback", "/api/playback/status"),
  poll("model", "/api/model"),
  poll("osc", "/api/osc"),
]);

const pollSlow = () => Promise.all([
  poll("status", "/api/status"),
  poll("session", "/api/session", (d) => d.session),
  poll("recording", "/api/recording/status"),
]);

function startFallback() {
  if (fallbackTimers.length) return;
  onConnectionChange(false);
  pollFast();
  pollSlow();
  fallbackTimers.push(setInterval(async () => {
    const [live] = await pollFast();
    // The API itself is unreachable — surface it rather than showing stale data.
    onConnectionChange(false, live ? null : "API injoignable");
  }, 400));
  fallbackTimers.push(setInterval(pollSlow, 1000));
}

function stopFallback() {
  fallbackTimers.forEach(clearInterval);
  fallbackTimers = [];
}

export function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/api/ws`);

  ws.onopen = () => {
    stopFallback();
    onConnectionChange(true);
  };

  ws.onmessage = (ev) => {
    let snapshot;
    try { snapshot = JSON.parse(ev.data); } catch { return; }
    ingest(snapshot);
  };

  ws.onclose = () => {
    startFallback();
    setTimeout(connect, 1000);
  };

  ws.onerror = () => { try { ws.close(); } catch { /* already closing */ } };
}
