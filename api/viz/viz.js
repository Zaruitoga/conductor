// viz.js — 3D visualiser served at /viz/.
//
// Two channels, two roles:
//   • the downstream stream (WS_PORT, see GET /api/config) drives the wheel:
//     `type: "frame"` messages carry the model's pose and signals;
//   • the panel snapshot (/api/ws, ~4 Hz) drives observation and playback state.
// Commands stay REST, exactly like the control panel.

import * as THREE from './vendor/three.module.js';
import { OrbitControls } from './vendor/OrbitControls.js';
import { mountVideo } from './video.js';
import { PoseCursor } from './sweep.js';

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

// ── The take's video, slave to the replay (video.js + sync-clock.js) ────────
// Three hooks, called below where the information already passes: a frame, a
// reset, and the playback state. Which take it shows is decided here, because
// the session tree — with both anchors and the stored `video_file` — lives here.
const takeVideo = mountVideo($("stage"));

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

// ?types=frame,meta — the wheel only needs the model's frames, and the server
// would otherwise also send gyro/game_rv/super_0/heartbeat: several times the
// messages to parse for nothing. `meta` is added for the reset alone: it is the
// instant the replay's timeline changes direction (a pass starting, a loop
// turning over, a jump landing) and the one moment the video has to move with it
// rather than wait for a drift to build. It is rare by construction, so it costs
// nothing to carry.
connect(`${wsProto}://${location.hostname}:${cfg.ws_port}/?types=frame,meta`, {
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
    if (d.type === "meta") { takeVideo.onMeta(d); return; }
    if (d.type !== "frame") return;
    packetCount++;
    // A hand on the cursor outranks the replay: a packet already in flight, or
    // a pause not yet acknowledged, must not drag the wheel off the instant
    // being looked at. Counted above all the same — the stream is still there.
    if (sweeping) return;
    // The video is slave to this `t` and never to `playback.elapsed_s`: the
    // snapshot is 4 Hz rounded to a tenth, which is 72° of wheel at two turns a
    // second.
    takeVideo.onFrame(d);

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
  $("pb-status").textContent = p.active
    ? `${p.paused ? "En pause" : "Lecture"} ${p.session}/${p.take} — ` +
      `${p.elapsed_s}/${p.total_s}s (×${p.speed}${p.loop ? ", boucle" : ""})`
    : "Inactif.";

  $("pb-pause").textContent = p.paused ? "Reprendre" : "Pause";
  $("pb-start").disabled = p.active;
  $("pb-pause").disabled = !p.active;
  $("pb-stop").disabled = !p.active;
  playbackPaused = !!p.paused;
  playbackActive = !!p.active;
  playbackElapsed = p.active ? p.elapsed_s : 0;
  playbackTotal   = p.active ? p.total_s : 0;

  // A jump costs a warm-up and the snapshot is 4 Hz, so between letting go of
  // the cursor and the replay arriving there, `elapsed_s` still names the place
  // we came from. Showing it would snap the bar back and then jump — so the
  // target is held until the replay reaches it, or until it plainly is not
  // going to (a jump refused, a replay stopped meanwhile).
  if (pendingSeek) {
    const arrived = Math.abs(playbackElapsed - pendingSeek.t) < ARRIVED_S;
    if (!p.active || arrived || Date.now() - pendingSeek.at > HOLD_MS) {
      pendingSeek = null;
      // The replay owns the bar again. On a take that stopped playing instead,
      // the cursor is left where the hand put it — `Lire ici` starts there.
      if (p.active) cursorT = null;
    }
  }
  renderScrub();

  // State, speed and pause — never the time. A replay owns the picture while it
  // runs; the selector only decides what is preloaded in between, so that the
  // first second of a replay is not played against an empty element.
  takeVideo.onPlayback(p);
  if (p.active && p.session && p.take) showTake(p.session, p.take);
  else showTake($("pb-session").value, $("pb-take").value);
}

let playbackPaused  = false;
let playbackActive  = false;
let playbackElapsed = 0;
let playbackTotal   = 0;

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

// ── Le balayage : parcourir le take au curseur, sans rien produire ──────────
//
// The bar is the cursor. Dragging it reads the take's pose track and nothing
// else: no frame, no event, no OSC, and neither `LiveMonitor` nor the scope
// sees a thing (ADR 0004). What moves is the wheel here and the picture in the
// inset, because both are drawn by this page from what it read.
//
// Grabbing it **holds a running replay**, and that is the whole of "chercher
// n'est pas jouer": left running, the replay would go on feeding Live from a
// place nobody is looking at while its frames fought this cursor for the wheel.
// Letting go is what resumes — at the instant found, through the jump, which is
// what keeps the wheel from teleporting (the warm-up seeds its position from
// this very track, storage/seek.py).
const cursor = new PoseCursor();

// The bar in the panel (api/static/js/panels/playback.js) answers to the same
// four numbers, and the two pages share no module by design — the viz has no
// build step and reimplements even its `api()` helper. Named here and there so
// that tuning one and not the other is *visible* rather than buried in a
// literal: how close the replay must get before the bar stops holding the
// target, how long it is held at all, how long after the last key the target is
// sent, and what a key moves by.
const ARRIVED_S     = 1.5;
const HOLD_MS       = 4000;
const KEY_COMMIT_MS = 300;
const KEY_STEP = { ArrowLeft: -1, ArrowRight: 1, PageDown: -10, PageUp: 10 };

let sweeping    = false;   // a hand is on the bar — keyboard included
let cursorT     = null;    // where it left the cursor, in take seconds
let metaTotal   = 0;       // the take's length as the session tree knows it
let resumeAfter = false;   // the replay was running when the hand came down
let pendingSeek = null;    // {t, at}: the target, until the replay reaches it
let keyCommit   = null;

const fmtS = (s) => `${(s || 0).toFixed(1).replace(".", ",")} s`;

const takeSeconds = (t) =>
  t && t.last_ts_rx_us > t.first_ts_rx_us
    ? (t.last_ts_rx_us - t.first_ts_rx_us) / 1e6
    : 0;

const takeDuration = (t) => takeSeconds(t).toFixed(1);

/**
 * The bar's scale.
 *
 * Three sources, in order of how much they know: a running replay counts the
 * take's own rows, a finished track ends exactly where the take does, and the
 * session tree's timestamps are an estimate — good enough to scale a bar with
 * before either of the other two exists.
 */
function totalS() {
  if (playbackActive && playbackTotal > 0) return playbackTotal;
  if (cursor.complete && cursor.limitS > 0) return cursor.limitS;
  return metaTotal;
}

/** What the bar shows: the cursor while it means something, else the replay. */
function shownT() {
  if (cursorT !== null) return cursorT;
  return playbackActive ? playbackElapsed : 0;
}

function renderScrub() {
  const total = totalS();
  const t     = shownT();
  const pct   = total > 0 ? Math.max(0, Math.min(100, (t / total) * 100)) : 0;
  const bar   = $("pb-scrub");

  $("pb-bar").style.width = `${pct}%`;
  $("pb-head").style.left = `${pct}%`;
  // Where the pose track stops. Past it the sweep has nothing to draw, which is
  // a fact about the computation and not about the take — so it is shown rather
  // than left to be discovered by a cursor that will not go further.
  const limit = cursor.limitS;
  $("pb-ready").style.width =
    total > 0 && limit > 0 ? `${Math.min(100, (limit / total) * 100)}%` : "0%";

  $("pb-cursor").textContent = total > 0 ? `${fmtS(t)} / ${fmtS(total)}` : "—";
  $("pb-track").textContent  = trackLabel();

  bar.setAttribute("aria-valuemax", total.toFixed(1));
  bar.setAttribute("aria-valuenow", t.toFixed(1));
  bar.setAttribute("aria-valuetext", fmtS(t));
  bar.setAttribute("aria-disabled", total > 0 ? "false" : "true");

  // A cursor placed on a take nobody is playing is a request: play *there*.
  // The button says so rather than leaving it to be discovered.
  const start = $("pb-start");
  const label = !playbackActive && cursorT !== null ? "Lire ici" : "Lire";
  if (start.textContent !== label) start.textContent = label;
}

function trackLabel() {
  const info = cursor.info;
  if (!info) return "";
  if (info.status === "failed") return `piste indisponible (${info.error || "erreur"})`;
  // Reported, never repaired behind the operator's back (ADR 0004): the poses
  // are usable, they are simply at the scale of another wheel.
  if (cursor.geometryMismatch) return "piste calculée à une autre géométrie";
  if (!info.complete) return `piste calculée jusqu'à ${fmtS(cursor.limitS)}`;
  return "";
}

/** The wheel, moved by the cursor instead of by a frame. */
function applyPose(p) {
  if (!p || p.qw === null) return;
  sampleQ.set(p.qx, p.qy, p.qz, p.qw);
  // A take recorded without a gyro has no horizontal position at all — null
  // here, and the wheel turns on the spot rather than being drawn at an origin
  // it never occupied.
  sampleP.set(p.x ?? 0, p.y ?? 0, p.z ?? 0);
  hasSample = true;
}

// A chunk landing, or the limit moving forward while the computation catches
// up: both mean the instant under the cursor may be drawable now when it was
// not a moment ago. This is what makes a take whose track is still filling
// sweepable *up to the limit reached*, and alive as that limit moves.
cursor.onChange = () => {
  if (cursorT !== null) applyPose(cursor.poseAt(cursorT));
  renderScrub();
};

function moveCursor(tS) {
  cursorT = cursor.clamp(Math.max(0, Math.min(totalS() || tS, tS)));
  applyPose(cursor.poseAt(cursorT));
  takeVideo.scrub(cursorT);
  renderScrub();
}

/** Take the picture over. Idempotent: a drag and a key press both come here. */
async function startSweep() {
  if (sweeping) return;
  sweeping = true;
  $("pb-scrub").classList.add("sweeping");
  resumeAfter = playbackActive && !playbackPaused;
  if (!resumeAfter) return;
  try {
    await api("POST", "/api/playback/pause");
  } catch (e) {
    resumeAfter = false;
    showError(e.message);
  }
}

/**
 * Let go: the instant found becomes the instant played.
 *
 * The seek is what carries the warm-up, so the wheel arrives where the sweep
 * left it instead of teleporting.  On a take nobody was playing there is no
 * replay to seek — the cursor simply stays, and `Lire` starts there.
 */
async function endSweep() {
  if (!sweeping) return;
  sweeping = false;
  $("pb-scrub").classList.remove("sweeping");
  takeVideo.endScrub();

  const t = cursorT;
  if (t === null || !playbackActive) { renderScrub(); return; }
  pendingSeek = { t, at: Date.now() };
  try {
    await api("POST", "/api/playback/seek", { t });
    // A seek is honoured while paused — deliberately, so its cost lands on the
    // drag rather than on the play button (storage/playback_engine.py).
    if (resumeAfter) await api("POST", "/api/playback/resume");
  } catch (e) {
    pendingSeek = null;
    showError(e.message);
  }
  renderScrub();
}

// ── The bar's own gestures ──────────────────────────────────────────────────
const scrubEl = $("pb-scrub");

function tFromEvent(e) {
  const r = scrubEl.getBoundingClientRect();
  const frac = r.width > 0 ? (e.clientX - r.left) / r.width : 0;
  return Math.max(0, Math.min(1, frac)) * totalS();
}

scrubEl.addEventListener("pointerdown", (e) => {
  if (!totalS()) return;
  e.preventDefault();
  scrubEl.setPointerCapture(e.pointerId);
  scrubEl.focus();
  startSweep();               // not awaited: the picture must not wait on REST
  moveCursor(tFromEvent(e));
});
scrubEl.addEventListener("pointermove", (e) => {
  if (sweeping) moveCursor(tFromEvent(e));
});
scrubEl.addEventListener("pointerup", endSweep);
scrubEl.addEventListener("pointercancel", endSweep);

// Arrows for the same gesture without a mouse. The commit is trailing — a key
// held down produces a stream of moves and only the last is worth a warm-up,
// the same reasoning `PlaybackEngine.seek` applies to a drag.
scrubEl.addEventListener("keydown", (e) => {
  const total = totalS();
  if (!total) return;
  let target = null;
  if (e.key in KEY_STEP) {
    const step = KEY_STEP[e.key] * (e.shiftKey ? 0.1 : 1);
    target = shownT() + step;
  } else if (e.key === "Home") {
    target = 0;
  } else if (e.key === "End") {
    target = total;
  } else {
    return;
  }
  e.preventDefault();
  startSweep();
  moveCursor(target);
  clearTimeout(keyCommit);
  keyCommit = setTimeout(endSweep, KEY_COMMIT_MS);
});

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
    ? [t.title, t.performer, `${t.packet_count} paquets`, alignmentLabel(t)]
        .filter(Boolean).join(" · ")
    : "";
  // The query is what /align/ reads at boot and then keeps true itself, so this
  // link stays a valid address after that page has been used.
  const link = $("pb-align");
  link.href = t
    ? `/align/?session=${encodeURIComponent(session.name)}&take=${encodeURIComponent(t.name)}`
    : "/align/";
  // A replay owns the picture while it runs — picking another take in the list
  // must not yank the video off the take being played.
  if (!playbackActive) showTake($("pb-session").value, $("pb-take").value);
}

// Derived from stored data alone, exactly as /align/'s pill is: no `video_file`
// ⇒ no video; anchors absent ⇒ not aligned; both ⇒ aligned. Nothing here is a
// detection verdict, which ADR 0001 excludes.
function alignmentLabel(t) {
  if (!t.video_file) return "sans vidéo";
  return Number.isFinite(t.onset_imu_s) && Number.isFinite(t.onset_video_s)
    ? "aligné" : "non aligné";
}

// What the video shows. The tree is the only place both anchors and the stored
// `video_file` are known, so the choice is made here and `video.js` is handed a
// take rather than left to look one up.
let missingKey = null;
let shownKey   = null;

function showTake(session, take) {
  const s = sessionTree.find((x) => x.name === session);
  const t = s && s.takes.find((x) => x.name === take);
  if (!t) {
    // A take recorded since this tree was loaded — a replay can perfectly well
    // name one. Refresh once per unknown take, not on every 4 Hz snapshot.
    const k = `${session}/${take}`;
    if (session && take && k !== missingKey) { missingKey = k; refreshSessions(); }
    takeVideo.setTake(null);
    if (shownKey !== null) { shownKey = null; forgetSweep(); }
    return;
  }
  missingKey = null;
  takeVideo.setTake({
    session, take,
    video_file:    t.video_file,
    onset_imu_s:   t.onset_imu_s,
    onset_video_s: t.onset_video_s,
  });

  const key = `${session}/${take}`;
  if (key === shownKey) return;
  shownKey  = key;
  forgetSweep();
  metaTotal = takeSeconds(t);
  // Opening the take is what makes it sweepable: the first request starts the
  // computation for a take that has no track yet, and the sweep is alive to
  // whatever it has reached (ADR 0004) rather than waiting for the end.
  cursor.open(session, take);
  renderScrub();
}

/** A different take is on screen: the cursor named an instant of another one. */
function forgetSweep() {
  cursorT     = null;
  pendingSeek = null;
  metaTotal   = 0;
  clearTimeout(keyCommit);
  cursor.close();
  renderScrub();
}

$("pb-refresh").onclick = refreshSessions;
$("pb-session").onchange = populateTakes;
$("pb-take").onchange = updateTakeMeta;

$("pb-start").onclick = async () => {
  const session = $("pb-session").value;
  const take = $("pb-take").value;
  if (!session || !take) { showError("Sélectionne une session et un take."); return; }
  // The cursor a sweep left on this take, handed to `start` rather than jumped
  // to straight after it: starting at row 0 and seeking would play the take's
  // opening for the length of a round trip — the wheel back at the origin, a
  // burst of frames and of OSC from a place nobody asked for.
  const from = cursorT;
  try {
    await api("POST", "/api/playback/start", {
      session, take,
      speed: parseFloat($("pb-speed").value) || 1,
      loop: $("pb-loop").checked,
      ...(from === null ? {} : { start_s: from }),
    });
    if (from !== null) pendingSeek = { t: from, at: Date.now() };
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
