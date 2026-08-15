// viz.js — 3D visualiser served at /viz/.
//
// Two channels, two roles:
//   • the downstream stream (WS_PORT, see GET /api/config) drives the wheel:
//     `type: "frame"` messages carry the model's pose and signals;
//   • the panel snapshot (/api/ws, ~4 Hz) drives observation and playback state.
// Commands stay REST, exactly like the control panel.

import * as THREE from './vendor/three.module.js';
import { OrbitControls } from './vendor/OrbitControls.js';

const $ = (id) => document.getElementById(id);
const RECONNECT_MS = 1000;

// ── Configuration (ports + geometry come from the backend) ──────────────────
const DEFAULT_CFG = { ws_port: 8081, geometry: { R_TORE: 1.0, r_TORE: 0.05 } };

let cfg = DEFAULT_CFG;
try {
  cfg = await (await fetch("/api/config")).json();
} catch {
  console.warn("GET /api/config failed — falling back to defaults", DEFAULT_CFG);
}
const R_TORE = cfg.geometry.R_TORE;
const r_TORE = cfg.geometry.r_TORE;

// ── HTTP helper (same contract as the panel's js/api.js) ────────────────────
async function api(method, path, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  let data = null;
  try { data = await res.json(); } catch { /* no body */ }
  if (!res.ok) throw new Error((data && data.detail) || res.statusText);
  return data;
}

function showError(msg) {
  $("pb-error").textContent = msg || "";
  if (msg) setTimeout(() => { $("pb-error").textContent = ""; }, 5000);
}

// ── Scene ───────────────────────────────────────────────────────────────────
const container = $("canvas-container");

const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(
  60, container.clientWidth / container.clientHeight, 0.1, 100,
);
camera.position.set(0, 1.5, 3);
camera.lookAt(0, 0, 0);

// Render cost is capped on purpose. A Retina screen reports devicePixelRatio 2,
// i.e. 4× the fragments, and MSAA on top of that; full-screen ground + grid make
// it fill-rate bound. Past ~1.5 the extra pixels buy nothing visible here, and a
// saturated main thread also stops draining the packet socket in time.
const dpr = window.devicePixelRatio || 1;
const renderer = new THREE.WebGLRenderer({ antialias: dpr < 2 });
renderer.setPixelRatio(Math.min(dpr, 1.5));
renderer.setSize(container.clientWidth, container.clientHeight);
container.appendChild(renderer.domElement);

const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;

new ResizeObserver(() => {
  const w = container.clientWidth, h = container.clientHeight;
  if (!w || !h) return;
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
  renderer.setSize(w, h);
}).observe(container);

scene.add(new THREE.AmbientLight(0x404040));
const light = new THREE.DirectionalLight(0xffffff, 1);
light.position.set(5, 10, 7.5);
scene.add(light);
scene.add(new THREE.AxesHelper(2));

const texture = new THREE.TextureLoader().load('checker.png');
texture.wrapS = THREE.RepeatWrapping;
texture.wrapT = THREE.RepeatWrapping;
texture.repeat.set(8, 1);

const imuPivot = new THREE.Object3D();
scene.add(imuPivot);

const roue = new THREE.Mesh(
  new THREE.TorusGeometry(R_TORE, r_TORE, 16, 100),
  new THREE.MeshStandardMaterial({ map: texture, side: THREE.DoubleSide }),
);
roue.rotation.z = Math.PI / 2;   // wheel plane in the pivot's local xy-plane
imuPivot.add(roue);

const ground = new THREE.Mesh(
  new THREE.PlaneGeometry(1000, 1000),
  new THREE.MeshBasicMaterial({ color: 0x888888, side: THREE.DoubleSide }),
);
ground.rotation.x = -Math.PI / 2;
scene.add(ground);

// Ground grid, kept under the wheel and snapped to whole cells so it reads as a
// fixed ground rather than a carpet dragged along — without it a followed wheel
// looks like it is spinning in place. Snapping to anything other than GRID_CELL
// would make the lines jump by a fraction of a cell.
const GRID_CELL = 2;   // metres — 200 m grid / 100 divisions
const grid = new THREE.GridHelper(200, 100, 0x666666, 0x777777);
grid.position.y = 0.001;   // avoid z-fighting with the ground
scene.add(grid);

// The pipeline works in a Z-up frame, Three.js is Y-up.
const qFix = new THREE.Quaternion()
  .setFromAxisAngle(new THREE.Vector3(1, 0, 0), -Math.PI / 2);

// ── Latest sample (only the newest one is ever drawn) ───────────────────────
const sampleQ = new THREE.Quaternion();
const sampleP = new THREE.Vector3();
let hasSample = false;
let packetCount = 0;   // computed packets since the last rate tick
let frameCount = 0;    // rendered frames since the last rate tick
let rateHz = 0;

// Camera follow: the wheel travels metres away from the origin (a coin-shaped
// trajectory alone is ~2 m across), so without this it simply leaves the frame.
// The orbit offset the user picked is preserved — only the target is moved.
const wheelPos = new THREE.Vector3();
const camDelta = new THREE.Vector3();

function animate() {
  requestAnimationFrame(animate);
  frameCount++;
  if (hasSample) {
    imuPivot.quaternion.copy(qFix).multiply(sampleQ);
    wheelPos.copy(sampleP).applyQuaternion(qFix);
    imuPivot.position.copy(wheelPos);
    grid.position.x = Math.round(wheelPos.x / GRID_CELL) * GRID_CELL;
    grid.position.z = Math.round(wheelPos.z / GRID_CELL) * GRID_CELL;

    if ($("follow").checked) {
      camDelta.subVectors(wheelPos, controls.target);
      camera.position.add(camDelta);
      controls.target.copy(wheelPos);
    }
  }
  controls.update();
  renderer.render(scene, camera);
}
animate();

$("recenter").onclick = () => {
  const target = hasSample ? wheelPos : new THREE.Vector3();
  controls.target.copy(target);
  camera.position.set(target.x, target.y + 1.5, target.z + 3);
};

// ── WebSocket helper with automatic reconnection ────────────────────────────
function connect(url, { onMessage, onOpen, onClose }) {
  const open = () => {
    const ws = new WebSocket(url);
    ws.onopen = () => onOpen && onOpen();
    ws.onmessage = onMessage;
    ws.onerror = () => ws.close();
    ws.onclose = () => {
      if (onClose) onClose();
      setTimeout(open, RECONNECT_MS);
    };
  };
  open();
}

function setDot(id, cls, textId, text) {
  $(id).className = `dot ${cls}`.trim();
  $(textId).textContent = text;
}

// ── Channel 1: packet stream (wheel motion) ─────────────────────────────────
const wsProto = location.protocol === "https:" ? "wss" : "ws";

// ?types=frame — the wheel only needs the model's frames, and the server would
// otherwise also send gyro/game_rv/super_0/heartbeat: several times the messages
// to parse for nothing.
connect(`${wsProto}://${location.hostname}:${cfg.ws_port}/?types=frame`, {
  onOpen: () => setDot("stream-dot", "ok", "stream-text",
                       `flux 3D — port ${cfg.ws_port}`),
  onClose: () => {
    setDot("stream-dot", "bad", "stream-text", "flux 3D déconnecté");
    hasSample = false;
    rateHz = 0;
  },
  onMessage: (ev) => {
    let d;
    try { d = JSON.parse(ev.data); } catch { return; }
    if (d.type !== "frame") return;
    packetCount++;

    // `pose` is the geometric state, always present in a frame — as opposed to
    // `signals`, whose contents depend on how the ESP is configured. Position
    // is null until the model has an angular velocity to integrate, so a wheel
    // configured without a gyro still renders its orientation.
    const p = d.pose;
    if (!p || p.qw === undefined) return;
    sampleQ.set(p.qx, p.qy, p.qz, p.qw);
    sampleP.set(p.x ?? 0, p.y ?? 0, p.z ?? 0);
    latestSignals = d.signals || {};
    hasSample = true;
  },
});

let latestSignals = {};

// ── Channel 2: panel snapshot (health, session, playback) ───────────────────
connect(`${wsProto}://${location.host}/api/ws`, {
  onOpen: () => setDot("panel-dot", "ok", "panel-text", "état connecté"),
  onClose: () => {
    setDot("panel-dot", "bad", "panel-text", "état déconnecté");
    setDot("health-dot", "", "health-text", "ESP —");
  },
  onMessage: (ev) => {
    let snap;
    try { snap = JSON.parse(ev.data); } catch { return; }
    renderHealth(snap.health);
    renderSession(snap.session, snap.recording);
    renderPlayback(snap.playback);
  },
});

// ── Rendering of the snapshot ───────────────────────────────────────────────
const HEALTH_DOT = { online: "ok", degraded: "warn", offline: "bad" };

function renderHealth(h) {
  if (!h) return;
  const bits = [`ESP ${h.state}`];
  // The block is named `heartbeat` (see EspHealth.snapshot); this used to read
  // `h.telemetry`, so the battery never appeared here.
  if (h.heartbeat && typeof h.heartbeat.battery_pct === "number") {
    bits.push(`${h.heartbeat.battery_pct.toFixed(0)} %`);
  }
  setDot("health-dot", HEALTH_DOT[h.state] || "", "health-text", bits.join(" · "));
}

function renderSession(s, rec) {
  if (!s) {
    $("session-info").textContent = "Aucune session ouverte.";
    return;
  }
  const parts = [s.title || s.name, `${(s.takes || []).length} take(s)`];
  if (rec && rec.active) parts.push(`● REC ${rec.take} (${rec.packet_count})`);
  $("session-info").textContent = parts.join(" · ");
}

function renderPlayback(p) {
  if (!p) return;
  $("pb-bar").style.width = `${p.percent}%`;
  $("pb-status").textContent = p.active
    ? `${p.paused ? "En pause" : "Lecture"} ${p.session}/${p.take} — ` +
      `${p.elapsed_s}/${p.total_s}s (×${p.speed}${p.loop ? ", boucle" : ""})`
    : "Inactif.";

  $("pb-pause").textContent = p.paused ? "Reprendre" : "Pause";
  $("pb-start").disabled = p.active;
  $("pb-pause").disabled = !p.active;
  $("pb-stop").disabled = !p.active;
  playbackPaused = !!p.paused;
}

let playbackPaused = false;

// ── HUD (throttled: the stream runs far faster than the eye) ────────────────
let lastTick = performance.now();
setInterval(() => {
  const now = performance.now();
  const elapsed = (now - lastTick) / 1000;   // the timer can fire late
  lastTick = now;
  rateHz = Math.round(packetCount / elapsed);
  const fps = Math.round(frameCount / elapsed);
  packetCount = 0;
  frameCount = 0;
  $("hud-rate").textContent = `${rateHz} Hz (frames) · ${fps} fps`;
  if (hasSample) {
    $("hud-pos").textContent =
      `P: x ${sampleP.x.toFixed(3)}  y ${sampleP.y.toFixed(3)}  z ${sampleP.z.toFixed(3)}`;
    // A couple of signals worth seeing while the wheel is moving. Which ones
    // exist depends on the ESP configuration, so each is shown only if present.
    $("hud-quat").textContent = [
      fmtSignal("lean_deg", "incl", "°"),
      fmtSignal("speed_ms", "v", " m/s"),
      fmtSignal("spin_rate_dps", "ω", "°/s"),
    ].filter(Boolean).join("   ") || "—";
  } else {
    $("hud-pos").textContent = "P: —";
    $("hud-quat").textContent = "Q: —";
  }
}, 1000);

function fmtSignal(name, label, unit) {
  const v = latestSignals[name];
  return typeof v === "number" ? `${label} ${v.toFixed(1)}${unit}` : "";
}

// ── Playback controls (REST; the engine has no seek, hence no scrubbing) ────
const takeDuration = (t) =>
  t.last_ts_rx_us > t.first_ts_rx_us
    ? ((t.last_ts_rx_us - t.first_ts_rx_us) / 1e6).toFixed(1)
    : "0.0";

let sessionTree = [];

async function refreshSessions() {
  try {
    const { sessions } = await api("GET", "/api/sessions");
    sessionTree = sessions;
  } catch (e) {
    showError(e.message);
    return;
  }
  const sel = $("pb-session");
  const prev = sel.value;
  sel.innerHTML = "";
  for (const s of sessionTree) {
    const o = document.createElement("option");
    o.value = s.name;
    o.textContent = `${s.title || s.name} (${s.takes.length})`;
    sel.appendChild(o);
  }
  if (sessionTree.some((s) => s.name === prev)) sel.value = prev;
  if (!sessionTree.length) {
    const o = document.createElement("option");
    o.textContent = "(aucune session)";
    o.disabled = true;
    sel.appendChild(o);
  }
  populateTakes();
}

function populateTakes() {
  const session = sessionTree.find((s) => s.name === $("pb-session").value);
  const sel = $("pb-take");
  const prev = sel.value;
  sel.innerHTML = "";
  const takes = session ? session.takes : [];
  for (const t of takes) {
    const o = document.createElement("option");
    o.value = t.name;
    o.textContent = `${t.name} — ${takeDuration(t)}s`;
    sel.appendChild(o);
  }
  if (takes.some((t) => t.name === prev)) sel.value = prev;
  if (!takes.length) {
    const o = document.createElement("option");
    o.textContent = "(aucun take)";
    o.disabled = true;
    sel.appendChild(o);
  }
  updateTakeMeta();
}

function updateTakeMeta() {
  const session = sessionTree.find((s) => s.name === $("pb-session").value);
  const t = session && session.takes.find((x) => x.name === $("pb-take").value);
  $("pb-take-meta").textContent = t
    ? [t.title, t.performer, `${t.packet_count} paquets`].filter(Boolean).join(" · ")
    : "";
  // The query is what /align/ reads at boot and then keeps true itself, so this
  // link stays a valid address after that page has been used.
  const link = $("pb-align");
  link.href = t
    ? `/align/?session=${encodeURIComponent(session.name)}&take=${encodeURIComponent(t.name)}`
    : "/align/";
}

$("pb-refresh").onclick = refreshSessions;
$("pb-session").onchange = populateTakes;
$("pb-take").onchange = updateTakeMeta;

$("pb-start").onclick = async () => {
  const session = $("pb-session").value;
  const take = $("pb-take").value;
  if (!session || !take) { showError("Sélectionne une session et un take."); return; }
  try {
    await api("POST", "/api/playback/start", {
      session, take,
      speed: parseFloat($("pb-speed").value) || 1,
      loop: $("pb-loop").checked,
    });
    showError("");
  } catch (e) { showError(e.message); }
};

$("pb-pause").onclick = async () => {
  try {
    await api("POST", playbackPaused ? "/api/playback/resume" : "/api/playback/pause");
  } catch (e) { showError(e.message); }
};

$("pb-stop").onclick = async () => {
  try { await api("POST", "/api/playback/stop"); }
  catch (e) { showError(e.message); }
};

refreshSessions();
