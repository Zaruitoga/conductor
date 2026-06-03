"use strict";

const SLOT_NAMES = [
  "GYRO", "ACCEL", "MAG", "LINEAR_ACCEL",
  "RV", "GEO_RV", "GAME_RV", "ARVR_RV",
];

const $ = (id) => document.getElementById(id);

// ── HTTP helper ────────────────────────────────────────────────────────────
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
    const msg = (data && data.detail) || res.statusText;
    throw new Error(msg);
  }
  return data;
}

function toast(msg, kind = "ok") {
  const t = $("toast");
  t.textContent = msg;
  t.className = kind;
  setTimeout(() => { t.textContent = ""; t.className = ""; }, 4000);
}

// Wrap an action: show success/error in the toast.
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

// ── Module 1+banner: status & ESP state ────────────────────────────────────
let lastRx = null;

async function pollStatus() {
  try {
    const s = await api("GET", "/api/status");
    $("mode-badge").textContent = s.mode;
    $("mode-badge").className = "mode " + s.mode;

    const rx = s.udp.rx;
    const receiving = lastRx !== null && rx > lastRx;
    lastRx = rx;

    const dot = $("conn-dot");
    if (receiving) {
      dot.className = "dot ok";
      $("conn-text").textContent = "Données reçues";
    } else if (rx > 0) {
      dot.className = "dot bad";
      $("conn-text").textContent = "Silence (pas de nouveaux paquets)";
    } else {
      dot.className = "dot";
      $("conn-text").textContent = "En attente de l'ESP32";
    }
    $("status-detail").textContent =
      `rx:${rx} err:${s.udp.errors} esp:${s.udp.last_esp_ip || "?"} ` +
      `queue:${s.queue_depth} ws:${s.ws.clients}`;
  } catch (e) {
    $("conn-dot").className = "dot bad";
    $("conn-text").textContent = "API injoignable";
  }
}

async function refreshEspState() {
  const box = $("esp-state");
  box.textContent = "Chargement…";
  try {
    const s = await api("GET", "/api/esp/state");
    if (!s.reachable) {
      box.textContent = "ESP32 injoignable (pas d'ACK).";
      return;
    }
    const lines = [`host_ip = ${s.host}`];
    for (const x of s.simples) {
      const name = SLOT_NAMES[x.slot] || "?";
      lines.push(`simple[${x.slot}] ${x.enabled ? "ON " : "off"} ${x.rate_hz}Hz  ${name}`);
    }
    for (const x of s.supers) {
      if (!x.active) continue;
      const names = x.deps.map((d) => SLOT_NAMES[d] || d).join(",");
      lines.push(`super[${x.slot}] deps=[${names}] skip=${x.skip_ratio} payload=${x.payload_sz}B`);
    }
    box.textContent = lines.join("\n");
  } catch (e) {
    box.textContent = "Erreur: " + e.message;
  }
}

// ── Module 3: recording ─────────────────────────────────────────────────────
async function pollRecording() {
  try {
    const s = await api("GET", "/api/recording/status");
    $("rec-status").textContent = s.active
      ? `● REC  ${s.session}  (${s.packet_count} paquets)`
      : "Inactif.";
  } catch { /* ignore */ }
}

// ── Module 4: playback ──────────────────────────────────────────────────────
async function refreshSessions() {
  try {
    const { sessions } = await api("GET", "/api/sessions");
    const sel = $("session-select");
    const prev = sel.value;
    sel.innerHTML = "";
    for (const name of sessions) {
      const o = document.createElement("option");
      o.value = o.textContent = name;
      sel.appendChild(o);
    }
    if (sessions.includes(prev)) sel.value = prev;
    if (sessions.length === 0) {
      const o = document.createElement("option");
      o.textContent = "(aucune session)";
      o.disabled = true;
      sel.appendChild(o);
    }
  } catch { /* ignore */ }
}

async function pollPlayback() {
  try {
    const s = await api("GET", "/api/playback/status");
    $("playback-status").textContent = s.active ? "▶ Lecture en cours…" : "Inactif.";
  } catch { /* ignore */ }
}

// ── Wiring ──────────────────────────────────────────────────────────────────
function populateSlotSelect() {
  const sel = $("simple-slot");
  SLOT_NAMES.forEach((name, i) => {
    const o = document.createElement("option");
    o.value = i;
    o.textContent = `${i} — ${name}`;
    sel.appendChild(o);
  });
}

function wire() {
  $("esp-refresh").onclick = refreshEspState;

  $("host-set").onclick = action(async () => {
    const ip = $("host-ip").value.trim() || null;
    await api("POST", "/api/esp/host", { ip });
    refreshEspState();
  });

  $("simple-set").onclick = action(async () => {
    await api("POST", "/api/esp/simple", {
      slot: parseInt($("simple-slot").value, 10),
      enabled: $("simple-enabled").value === "true",
      hz: parseFloat($("simple-hz").value),
    });
    refreshEspState();
  });

  $("super-set").onclick = action(async () => {
    const deps = $("super-deps").value.split(",")
      .map((d) => parseInt(d.trim(), 10)).filter((d) => !Number.isNaN(d));
    await api("POST", "/api/esp/super", {
      slot: parseInt($("super-slot").value, 10),
      deps,
      skip: parseInt($("super-skip").value, 10),
    });
    refreshEspState();
  });

  $("super-del").onclick = action(async () => {
    await api("DELETE", "/api/esp/super/" + parseInt($("super-slot").value, 10));
    refreshEspState();
  });

  $("rec-start").onclick  = action(() => api("POST", "/api/recording/start").then(pollRecording));
  $("rec-stop").onclick   = action(() => api("POST", "/api/recording/stop").then(pollRecording));
  $("rec-marker").onclick = action(() => api("POST", "/api/recording/marker"));

  $("session-refresh").onclick = refreshSessions;
  $("playback-start").onclick = action(async () => {
    await api("POST", "/api/playback/start", {
      name: $("session-select").value,
      speed: parseFloat($("playback-speed").value),
    });
    pollPlayback();
  });
  $("playback-stop").onclick = action(() => api("POST", "/api/playback/stop").then(pollPlayback));
}

function poll() {
  pollStatus();
  pollRecording();
  pollPlayback();
}

populateSlotSelect();
wire();
refreshSessions();
refreshEspState();
poll();
setInterval(poll, 1000);
