"use strict";

// ── View-only frontend ──────────────────────────────────────────────────────
// Observation (status/live/recording/playback/esp) is pushed by the backend
// over a WebSocket (/api/ws, ~4 Hz) and merely rendered here. Commands stay
// REST. If the socket drops, we fall back to REST polling until it reconnects.

const SLOT_NAMES = [
  "GYRO", "ACCEL", "MAG", "LINEAR_ACCEL",
  "RV", "GEO_RV", "GAME_RV", "ARVR_RV",
];

const $ = (id) => document.getElementById(id);
const fmt = (v) => (typeof v === "number" ? v.toFixed(3) : v);

// ── HTTP helper (commands + fallback polling) ───────────────────────────────
async function api(method, path, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  let data = null;
  try { data = await res.json(); } catch { /* no body */ }
  if (!res.ok) {
    throw new Error((data && data.detail) || res.statusText);
  }
  return data;
}

function toast(msg, kind = "ok") {
  const t = $("toast");
  t.textContent = msg;
  t.className = kind;
  setTimeout(() => { t.textContent = ""; t.className = ""; }, 4000);
}

// Wrap an async command: report success/error in the toast.
function action(fn) {
  return async (...args) => {
    try {
      const r = await fn(...args);
      toast("OK", "ok");
      return r;
    } catch (e) {
      toast(e.message, "bad");
    }
  };
}

// ── Render functions (pure: take a snapshot slice, update the DOM) ───────────
function renderStatus(s) {
  if (!s) return;
  $("mode-badge").textContent = s.mode;
  $("mode-badge").className = "mode " + s.mode;
  $("status-detail").textContent =
    `rx:${s.udp.rx} err:${s.udp.errors} esp:${s.udp.last_esp_ip || "?"} ` +
    `queue:${s.queue_depth} ws:${s.ws.clients}`;
}

function renderLive(live) {
  if (!live) return;

  const dot = $("conn-dot");
  if (live.connected) {
    dot.className = "dot ok";
    $("conn-text").textContent = `Données reçues (${live.age_ms} ms)`;
  } else {
    dot.className = "dot bad";
    $("conn-text").textContent =
      live.age_ms == null ? "En attente de données" : "Silence";
  }

  // Débit par type
  const tb = $("live-rates").tBodies[0];
  tb.innerHTML = "";
  const rates = Object.entries(live.rates || {});
  if (!rates.length) {
    const td = tb.insertRow().insertCell();
    td.colSpan = 2; td.className = "muted"; td.textContent = "—";
  } else {
    for (const [type, hz] of rates) {
      const tr = tb.insertRow();
      tr.insertCell().textContent = type;
      const c = tr.insertCell();
      c.textContent = hz + " Hz";
      c.className = "num";
    }
  }

  // Dernières valeurs
  const lines = [];
  if (live.battery_pct != null) lines.push(`batterie : ${live.battery_pct}%`);
  if (live.torus) {
    lines.push(`tore : x=${fmt(live.torus.px)} y=${fmt(live.torus.py)} z=${fmt(live.torus.pz)}`);
  }
  for (const [type, vals] of Object.entries(live.latest || {})) {
    if (type === "battery" || type === "computed") continue;
    const parts = Object.entries(vals).map(([k, v]) => `${k}=${fmt(v)}`);
    if (parts.length) lines.push(`${type}: ${parts.join("  ")}`);
  }
  $("live-values").textContent = lines.length ? lines.join("\n") : "—";
}

function renderRecording(s) {
  if (!s) return;
  $("rec-status").textContent = s.active
    ? `● REC  ${s.session}  (${s.packet_count} paquets)`
    : "Inactif.";
}

function renderPlayback(s) {
  if (!s) return;
  $("playback-bar").style.width = (s.active ? s.percent : 0) + "%";
  $("playback-status").textContent = s.active
    ? `▶ ${s.session} — ${s.elapsed_s}/${s.total_s}s (${s.percent}%)${s.loop ? " ⟳" : ""} ×${s.speed}`
    : "Inactif.";
}

// ── ESP32 état + contrôle (poussé dans le snapshot, dérivé des ACK) ──────────
// The ESP config only changes via our own commands, each of which ACKs the
// full state — so the backend caches it and pushes it here. We rebuild the
// widgets ONLY when the state actually changes, so a 4 Hz push never clobbers
// a value the user is typing into an Hz/deps field.
let lastEspJson = null;

function renderEspState(esp) {
  const json = JSON.stringify(esp ?? null);
  if (json === lastEspJson) return;
  lastEspJson = json;

  if (!esp) {
    $("esp-conn").textContent = "État ESP inconnu (aucune commande acquittée).";
    $("esp-host").textContent = "?";
    $("simple-rows").innerHTML = '<span class="muted">—</span>';
    $("super-rows").innerHTML = '<span class="muted">—</span>';
    return;
  }
  $("esp-conn").textContent = "Config ESP acquittée ✓";
  $("esp-host").textContent = esp.host;
  renderSimples(esp.simples);
  renderSupers(esp.supers);
}

function renderSimples(simples) {
  const box = $("simple-rows");
  box.innerHTML = "";
  const bySlot = {};
  (simples || []).forEach((x) => { bySlot[x.slot] = x; });

  for (let slot = 0; slot < 8; slot++) {
    const x = bySlot[slot];
    const row = document.createElement("div");
    row.className = "slot-row";

    const name = document.createElement("span");
    name.className = "slot-name";
    name.textContent = `${slot} ${SLOT_NAMES[slot]}`;

    const chk = document.createElement("input");
    chk.type = "checkbox";
    chk.checked = x ? x.enabled : false;
    const onLbl = document.createElement("label");
    onLbl.append(chk, " on");

    const hz = document.createElement("input");
    hz.type = "number"; hz.min = "1"; hz.step = "1"; hz.className = "hz-input";
    hz.value = x && x.rate_hz ? x.rate_hz : 50;
    const hzLbl = document.createElement("label");
    hzLbl.append(hz, " Hz");

    const btn = document.createElement("button");
    btn.textContent = "Appliquer";
    btn.onclick = action(() => api("POST", "/api/esp/simple", {
      slot, enabled: chk.checked, hz: parseFloat(hz.value),
    }));

    row.append(name, onLbl, hzLbl, btn);
    box.append(row);
  }
}

function renderSupers(supers) {
  const box = $("super-rows");
  box.innerHTML = "";
  const active = (supers || []).filter((s) => s.active);
  if (!active.length) {
    box.innerHTML = '<span class="muted">Aucun super-slot actif.</span>';
    return;
  }
  for (const s of active) {
    const row = document.createElement("div");
    row.className = "slot-row";
    const name = document.createElement("span");
    name.className = "slot-name wide";
    const deps = s.deps.map((d) => SLOT_NAMES[d] || d).join(",");
    name.textContent = `super[${s.slot}] deps=[${deps}] skip=${s.skip_ratio} ${s.payload_sz}B`;

    const del = document.createElement("button");
    del.textContent = "Supprimer";
    del.className = "danger";
    del.onclick = action(() => api("DELETE", "/api/esp/super/" + s.slot));

    row.append(name, del);
    box.append(row);
  }
}

// ── Présets ESP (localStorage) ──────────────────────────────────────────────
const PRESETS_KEY = "conductor_esp_presets";
const loadPresets = () => {
  try { return JSON.parse(localStorage.getItem(PRESETS_KEY)) || {}; }
  catch { return {}; }
};
const savePresets = (p) => localStorage.setItem(PRESETS_KEY, JSON.stringify(p));

// Built from the last ESP state pushed over the WS.
function currentEspState() {
  try { return JSON.parse(lastEspJson); } catch { return null; }
}

function savePreset(name) {
  if (!name) { toast("Donne un nom au préset", "bad"); return; }
  const esp = currentEspState();
  if (!esp) { toast("État ESP inconnu", "bad"); return; }
  const p = loadPresets();
  p[name] = {
    simples: esp.simples.map((s) => ({ slot: s.slot, enabled: s.enabled, hz: s.rate_hz || 50 })),
    supers: esp.supers.filter((s) => s.active).map((s) => ({
      slot: s.slot, deps: s.deps, skip: s.skip_ratio,
    })),
  };
  savePresets(p);
  renderPresets();
  toast("Préset enregistré", "ok");
}

function deletePreset(name) {
  const p = loadPresets();
  delete p[name];
  savePresets(p);
  renderPresets();
}

async function applyPreset(name, btn) {
  const preset = loadPresets()[name];
  if (!preset) return;
  btn.disabled = true;
  try {
    for (const s of preset.simples) {
      await api("POST", "/api/esp/simple", { slot: s.slot, enabled: s.enabled, hz: s.hz });
    }
    for (const s of preset.supers) {
      await api("POST", "/api/esp/super", { slot: s.slot, deps: s.deps, skip: s.skip });
    }
    toast("Préset appliqué", "ok");   // ESP widgets refresh on the next WS push
  } catch (e) {
    toast(e.message, "bad");
  } finally {
    btn.disabled = false;
  }
}

function renderPresets() {
  const box = $("preset-list");
  box.innerHTML = "";
  const presets = loadPresets();
  const names = Object.keys(presets);
  if (!names.length) {
    box.innerHTML = '<span class="muted">Aucun préset.</span>';
    return;
  }
  for (const name of names) {
    const row = document.createElement("div");
    row.className = "slot-row";
    const label = document.createElement("span");
    label.className = "slot-name wide";
    label.textContent = name;
    const apply = document.createElement("button");
    apply.textContent = "Appliquer";
    apply.onclick = () => applyPreset(name, apply);
    const del = document.createElement("button");
    del.textContent = "✕";
    del.className = "danger";
    del.onclick = () => deletePreset(name);
    row.append(label, apply, del);
    box.append(row);
  }
}

// ── Sessions (REST, on demand) ──────────────────────────────────────────────
let sessionMeta = {};

async function refreshSessions() {
  try {
    const { sessions } = await api("GET", "/api/sessions");
    sessionMeta = {};
    const sel = $("session-select");
    const prev = sel.value;
    sel.innerHTML = "";
    for (const s of sessions) {
      sessionMeta[s.name] = s;
      const o = document.createElement("option");
      o.value = s.name;
      o.textContent = s.duration_s != null
        ? `${s.name} — ${s.duration_s}s, ${s.packet_count} paq.${s.has_marker ? " ✓" : ""}`
        : s.name;
      sel.appendChild(o);
    }
    if (sessions.some((s) => s.name === prev)) sel.value = prev;
    if (!sessions.length) {
      const o = document.createElement("option");
      o.textContent = "(aucune session)";
      o.disabled = true;
      sel.appendChild(o);
    }
    updateSessionMeta();
  } catch { /* ignore */ }
}

function updateSessionMeta() {
  const m = sessionMeta[$("session-select").value];
  $("session-meta").textContent = (m && m.started_at)
    ? `Démarrée ${m.started_at} · ${m.duration_s}s · ${m.packet_count} paquets${m.has_marker ? " · marqueur ✓" : ""}`
    : "";
}

// ── REST polling fallback (only while the WS is down) ───────────────────────
async function pollStatus()    { try { renderStatus(await api("GET", "/api/status")); } catch { /**/ } }
async function pollRecording() { try { renderRecording(await api("GET", "/api/recording/status")); } catch { /**/ } }
async function pollPlayback()  { try { renderPlayback(await api("GET", "/api/playback/status")); } catch { /**/ } }
async function pollLive() {
  try { renderLive(await api("GET", "/api/live")); }
  catch {
    $("conn-dot").className = "dot bad";
    $("conn-text").textContent = "API injoignable";
  }
}

let fallbackTimers = [];
function startFallback() {
  if (fallbackTimers.length) return;
  pollStatus(); pollLive(); pollRecording(); pollPlayback();
  fallbackTimers.push(setInterval(() => { pollLive(); pollPlayback(); }, 400));
  fallbackTimers.push(setInterval(() => { pollStatus(); pollRecording(); }, 1000));
}
function stopFallback() {
  fallbackTimers.forEach(clearInterval);
  fallbackTimers = [];
}

// ── WebSocket push channel (primary) ────────────────────────────────────────
function connectPanelWS() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/api/ws`);

  ws.onopen = stopFallback;
  ws.onmessage = (ev) => {
    let s;
    try { s = JSON.parse(ev.data); } catch { return; }
    renderStatus(s.status);
    renderLive(s.live);
    renderRecording(s.recording);
    renderPlayback(s.playback);
    renderEspState(s.esp);
  };
  ws.onclose = () => {
    startFallback();                 // keep the panel live via REST…
    setTimeout(connectPanelWS, 1000); // …and retry the socket
  };
  ws.onerror = () => { try { ws.close(); } catch { /**/ } };
}

// ── Wiring (commands — all REST) ────────────────────────────────────────────
function wire() {
  $("host-set").onclick = action(() => {
    const ip = $("host-ip").value.trim() || null;
    return api("POST", "/api/esp/host", { ip });
  });

  $("super-add").onclick = action(() => {
    const deps = $("super-deps").value.split(",")
      .map((d) => parseInt(d.trim(), 10)).filter((d) => !Number.isNaN(d));
    return api("POST", "/api/esp/super", {
      slot: parseInt($("super-slot").value, 10),
      deps,
      skip: parseInt($("super-skip").value, 10),
    });
  });

  $("preset-save").onclick = () => savePreset($("preset-name").value.trim());

  $("rec-start").onclick  = action(() => api("POST", "/api/recording/start"));
  $("rec-stop").onclick   = action(() => api("POST", "/api/recording/stop").then(refreshSessions));
  $("rec-marker").onclick = action(() => api("POST", "/api/recording/marker"));

  $("session-refresh").onclick = refreshSessions;
  $("session-select").onchange = updateSessionMeta;

  document.querySelectorAll(".speed-preset").forEach((b) => {
    b.onclick = () => { $("playback-speed").value = b.dataset.speed; };
  });

  $("playback-start").onclick = action(() => api("POST", "/api/playback/start", {
    name: $("session-select").value,
    speed: parseFloat($("playback-speed").value),
    loop: $("playback-loop").checked,
  }));
  $("playback-stop").onclick = action(() => api("POST", "/api/playback/stop"));
}

// ── Boot ────────────────────────────────────────────────────────────────────
wire();
renderPresets();
refreshSessions();
connectPanelWS();
