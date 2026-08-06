// PROTOTYPE JETABLE (#9) — l'interface d'alignement, variante D.
//
// Née du grillage : A, B et C partageaient un modèle de tâche faux. Elles
// faisaient CORRIGER À L'ŒIL un instant que le serveur avait déjà calculé, ne
// donnaient aucun moyen d'atteindre l'endroit intéressant (pas le plus large :
// 1 s), et rien ne permettait de voir bouger 3 px — la résolution même sur
// laquelle #7 a calibré son seuil.
//
// D repose sur quatre décisions :
//   1. Côté IMU on CHOISIT parmi les candidats de la règle de #7 (2 à 4 par
//      take), on ne pointe pas. Le premier est retenu, ↑↓ passent au suivant.
//   2. Côté vidéo un scrubber pour arriver ; ←→ entrent d'eux-mêmes en mode
//      détail (frame par frame), sans touche de mode à apprendre.
//   3. Voir 3 px = comparateur à bascule : on épingle une frame de repos, on
//      maintient B pour la flasher. L'écart se cumule depuis le repos, donc un
//      départ mou finit par crever l'œil.
//   4. Une fois les deux ancres posées, une timeline de vérification s'ajoute en
//      bas — graduée dans le temps du take (celui de frame.t), pour répondre à
//      UNE question : la détection a-t-elle désigné le bon départ ? Si non
//      l'erreur est de plusieurs secondes : on change de candidat et on repose.
//
// Pas de roue 3D : le cadrage l'avait conclu, la maquette l'a confirmé.
//
//   &broken=onset        aucun candidat
//   &broken=unreadable   le fichier ne se décode pas
//   (le take 001 n'a ni vidéo ni flux gyro : deux états dégradés pour de vrai)

import { api, Stepper, drawCurve, fmt } from "./engine.js";

const BROKEN = new URLSearchParams(location.search).get("broken") || "";

const S = {
  takes: [], cur: null, data: null, stepper: null, video: null,
  pick: 0,          // index du candidat retenu
  vid: null,        // ancre vidéo = mediaTime MESURÉ (#10)
  pinned: null,     // { t, bitmap } — la frame de repos épinglée
  detail: false, blink: false, playing: false,
  seekTake: null,   // temps du take DEMANDÉ au transport, avant que le seek le borne
};

const $ = (s) => document.querySelector(s);
const key = (t) => `${t.session}/${t.take}`;
const cands = () => S.data?.candidates ?? [];
const imu = () => cands()[S.pick]?.t ?? null;
const noGyro = () => S.data && S.data.curve.length === 0;

// take ↔ vidéo : les deux ancres définissent la translation, rien d'autre.
const toVideo = (t) => S.cur?.aligned
  ? t - S.cur.aligned.onset_imu_s + S.cur.aligned.onset_video_s : null;
const toTake = (t) => S.cur?.aligned
  ? t - S.cur.aligned.onset_video_s + S.cur.aligned.onset_imu_s : null;

// ── Données ─────────────────────────────────────────────────────────────────
async function loadTakes() { S.takes = await api("/api/takes"); }

async function selectTake(t) {
  S.cur = t; S.vid = null; S.pick = 0; S.pinned = null;
  S.detail = false; S.playing = false; S.stepper = null;
  const q = `session=${encodeURIComponent(t.session)}&take=${encodeURIComponent(t.take)}`;
  S.data = await api(`/api/onset?${q}&broken=${BROKEN}`);
  if (!t.video_file && BROKEN !== "unreadable") { render(); return; }
  S.video.src = `/api/video?${q}&broken=${BROKEN}`;
  S.stepper = new Stepper(S.video).on(render);
  S.video.addEventListener("loadeddata", () => S.stepper.measure().then(render), { once: true });
  S.video.addEventListener("error", () => { S.stepper = null; render(); }, { once: true });
  render();
}

async function confirmAlign() {
  if (imu() == null || S.vid == null) return;
  await api("/api/align", {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ session: S.cur.session, take: S.cur.take,
                           onset_imu_s: imu(), onset_video_s: S.vid }),
  });
  await loadTakes();
  S.cur = S.takes.find((t) => key(t) === key(S.cur));
  render();
}

// ── Gestes ──────────────────────────────────────────────────────────────────
function enterDetail() { S.detail = true; }

// L'épinglée est capturée dans un canvas : un <video> n'affiche qu'une frame.
function pin() {
  if (!S.stepper) return;
  const c = document.createElement("canvas");
  c.width = S.video.videoWidth; c.height = S.video.videoHeight;
  c.getContext("2d").drawImage(S.video, 0, 0);
  S.pinned = { t: S.stepper.media, canvas: c };
  render();
}

function showBlink(on) {
  if (S.blink === on) return;
  S.blink = on && !!S.pinned;
  const el = $("#pinned");
  if (S.blink) {
    const g = el.getContext("2d");
    el.width = S.pinned.canvas.width; el.height = S.pinned.canvas.height;
    g.drawImage(S.pinned.canvas, 0, 0);
  }
  el.hidden = !S.blink;
  render();
}

function cycleCandidate(d) {
  if (!cands().length) return;
  S.pick = (S.pick + d + cands().length) % cands().length;
  render();
}

function togglePlay() {
  if (!S.stepper) return;
  S.seekTake = null;
  S.playing = S.video.paused;
  S.playing ? S.video.play() : S.video.pause();
  const tick = () => {
    if (S.video.paused) { S.playing = false; render(); return; }
    render(); requestAnimationFrame(tick);
  };
  tick();
}

// Position courante en temps vidéo — pendant la lecture c'est currentTime qui
// fait foi (rVFC ne sert qu'au pas-à-pas), en pause c'est le mediaTime mesuré.
const videoNow = () => !S.stepper ? null
  : (S.playing ? S.video.currentTime : S.stepper.media);

function initKeys() {
  addEventListener("keydown", (e) => {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    if (document.activeElement?.tagName === "INPUT") document.activeElement.blur();
    switch (e.key) {
      case "ArrowRight": case "ArrowLeft":
        e.preventDefault(); enterDetail(); S.seekTake = null;
        S.stepper?.step((e.key === "ArrowRight" ? 1 : -1) * (e.shiftKey ? 10 : 1)); break;
      case "ArrowUp":   e.preventDefault(); cycleCandidate(-1); break;
      case "ArrowDown": e.preventDefault(); cycleCandidate(1); break;
      case "e": case "E": e.preventDefault(); pin(); break;
      case "b": case "B": e.preventDefault(); showBlink(true); break;
      case "Enter": e.preventDefault(); S.vid = S.stepper?.media ?? null; confirmAlign(); break;
      case " ": e.preventDefault(); togglePlay(); break;
      case "Escape": S.detail = false; render(); break;
    }
  });
  addEventListener("keyup", (e) => { if (e.key === "b" || e.key === "B") showBlink(false); });
  addEventListener("blur", () => showBlink(false));
}

// ── Rendu ───────────────────────────────────────────────────────────────────
function stageFallback() {
  if (!S.cur) return "…";
  if (!S.cur.video_file && BROKEN !== "unreadable")
    return `<div class="fallback"><b>Pas de vidéo pour ce take.</b>
      <span>Copier le fichier dans <code>sessions/${S.cur.session}/takes/${S.cur.take}/</code>
      puis rafraîchir. Les candidats IMU sont proposés quand même : ils n'attendent
      pas la vidéo.</span></div>`;
  if (S.stepper === null && S.video?.error)
    return `<div class="fallback bad"><b>Le fichier ne se décode pas.</b>
      <span>${S.cur.video_file} — le navigateur refuse le flux. Rien à aligner tant
      qu'il n'est pas lisible.</span></div>`;
  return null;
}

const badge = (t) => t.aligned
  ? `<span class="pill ok">aligné · Δ ${(t.aligned.onset_video_s - t.aligned.onset_imu_s).toFixed(3)} s</span>`
  : t.video_file ? `<span class="pill">à aligner</span>` : `<span class="pill dim">sans vidéo</span>`;

function banner() {
  if (S.stepper?.rvfc !== false) return;
  const b = $("#banner");
  b.textContent = "⚠ requestVideoFrameCallback ne rappelle pas dans ce navigateur : repli sur "
    + "currentTime, cadence supposée. Ouvrir dans Chrome ou Safari pour juger le geste.";
  b.hidden = false; document.body.classList.add("warned");
  document.documentElement.style.setProperty("--banner", b.offsetHeight + "px");
}

function renderVideoZone() {
  const fb = stageFallback();
  $("#v").style.display = fb ? "none" : "";
  $("#stage").classList.toggle("empty", !!fb);
  $("#fallback").innerHTML = fb ?? "";
  $("#fallback").hidden = !fb;

  const st = S.stepper;
  $("#hud").hidden = !st;
  if (!st) return;
  $("#hud-time").textContent = `${st.media.toFixed(3)} s`;
  $("#hud-frame").textContent = `frame ≈ ${st.frameApprox}`;
  $("#hud-mode").textContent = S.detail ? "détail — frame par frame" : "navigation";
  $("#hud-mode").className = S.detail ? "chip on" : "chip";
  $("#hud-cadence").textContent =
    `cadence ${st.rvfc === false ? "supposée" : "mesurée"} ${(1 / st.dt).toFixed(2)} fps`
    + (st.spread ? ` (${(st.spread[0] * 1e3).toFixed(1)}–${(st.spread[1] * 1e3).toFixed(1)} ms)` : "")
    + ` · ${st.tries} aller-retour${st.tries > 1 ? "s" : ""}`;

  $("#pin-state").innerHTML = S.pinned
    ? `épinglée <b>${S.pinned.t.toFixed(3)} s</b> — maintenir <kbd>B</kbd> pour la flasher`
    : `<kbd>E</kbd> épingler une frame où la roue est au repos`;
  $("#blink-tag").hidden = !S.blink;

  const sc = $("#scrub");
  sc.max = S.video.duration || 0;
  if (document.activeElement !== sc) sc.value = videoNow() ?? 0;
}

function renderCurve() {
  if (!S.data) return;
  const markers = cands().map((c, i) => ({
    t: c.t, row: i,
    color: i === S.pick ? "#3fb950" : "#5c666f",
    wide: i === S.pick, dashed: i !== S.pick,
    label: `${i + 1}. ${c.t.toFixed(2)} s — repos ${c.silence_s} s${i === S.pick ? " ✓" : ""}`,
  }));
  const now = videoNow();
  if (now != null && S.cur?.aligned) {
    markers.push({ t: toTake(now), color: "#f85149", row: cands().length, label: "lecture" });
  }
  drawCurve($("#curve"), { curve: S.data.curve, t0: 0, t1: S.data.duration_s,
                           thr: S.data.silence_rad_s, markers });
}

function renderCandidates() {
  const box = $("#cand-list");
  if (noGyro()) {
    box.innerHTML = `<span class="warn">Aucun flux gyro enregistré dans ce take —
      pas d'ancre IMU possible.</span>`;
    return;
  }
  if (!cands().length) {
    box.innerHTML = `<span class="warn">Aucun candidat : le take ne contient pas de
      silence d'au moins ${S.data?.min_silence_s} s suivi d'un franchissement.</span>`;
    return;
  }
  box.innerHTML = cands().map((c, i) =>
    `<button class="cand ${i === S.pick ? "on" : ""}" data-i="${i}">
       <b>${c.t.toFixed(2)} s</b><span class="dim">repos ${c.silence_s} s</span></button>`).join("");
  box.querySelectorAll("[data-i]").forEach((b) =>
    b.onclick = () => { S.pick = +b.dataset.i; render(); });
}

// La timeline de vérification : deux objets superposés au même temps (celui du
// take, côté IMU) — la courbe au-dessus, le transport en dessous, avec la zone
// réellement couverte par la vidéo, qui ne couvre pas tout le take.
function renderVerify() {
  const on = !!S.cur?.aligned && !!S.stepper;
  $("#verify").hidden = !on;
  if (!on) return;
  const dur = S.data.duration_s;
  const v0 = toTake(0), v1 = toTake(S.video.duration || 0);
  $("#cover").style.left = `${Math.max(0, v0 / dur) * 100}%`;
  $("#cover").style.width = `${(Math.min(dur, v1) - Math.max(0, v0)) / dur * 100}%`;
  $("#cover-lbl").textContent =
    `vidéo : ${v0.toFixed(1)} s → ${v1.toFixed(1)} s du take (take ${dur.toFixed(1)} s)`;
  // Le curseur suit ce qu'on a DEMANDÉ, pas où la vidéo a atterri : au-delà de
  // sa fin le seek borne, et comparer la position bornée ne signalerait jamais
  // la sortie de couverture — le take déborde la vidéo des deux côtés.
  const cur = S.seekTake ?? toTake(videoNow() ?? 0);
  const t = $("#transport");
  t.max = dur;
  if (document.activeElement !== t) t.value = cur;
  $("#outside").hidden = cur >= v0 && cur <= v1;
  $("#play").textContent = S.playing ? "Pause" : "Lire";
}

function render() {
  banner();
  $("#state").innerHTML = S.cur ? badge(S.cur) : "";
  renderVideoZone();
  renderCurve();
  renderCandidates();
  renderVerify();
  $("#anchors").innerHTML =
    `ancre IMU <b>${fmt(imu())}</b>${cands().length > 1
      ? ` <span class="dim">(candidat ${S.pick + 1}/${cands().length})</span>` : ""}
     · ancre vidéo <b>${S.stepper ? S.stepper.media.toFixed(3) + " s" : "—"}</b>
     <span class="dim">${S.stepper?.rvfc === false ? "repli currentTime"
       : S.stepper ? "mediaTime mesuré" : ""}</span>`;
  $("#ok").disabled = !S.stepper || imu() == null;
  $("#ok").textContent = S.stepper && imu() != null
    ? `Confirmer · Δ ${(S.stepper.media - imu()).toFixed(3)} s` : "Confirmer l'alignement";
}

// ── Démarrage ───────────────────────────────────────────────────────────────
(async function boot() {
  S.video = $("#v");
  initKeys();

  $("#scrub").oninput = (e) => {
    S.detail = false; S.seekTake = null; S.stepper?.seek(+e.target.value);
  };
  $("#transport").oninput = (e) => {
    S.seekTake = +e.target.value; S.stepper?.seek(toVideo(S.seekTake));
  };
  $("#play").onclick = togglePlay;
  $("#ok").onclick = () => { S.vid = S.stepper?.media ?? null; confirmAlign(); };
  $("#pick").onchange = (e) => selectTake(S.takes[e.target.value]);
  $("#curve").onclick = (e) => {                       // cliquer un candidat sur la courbe
    if (!cands().length) return;
    const r = e.target.getBoundingClientRect();
    const t = (e.clientX - r.left) / r.width * S.data.duration_s;
    S.pick = cands().reduce((best, c, i) =>
      Math.abs(c.t - t) < Math.abs(cands()[best].t - t) ? i : best, 0);
    render();
  };
  addEventListener("resize", render);

  [["", "nominal"], ["onset", "sans candidat"], ["unreadable", "vidéo illisible"]]
    .forEach(([b, l]) => {
      const el = document.createElement("button");
      el.textContent = l; el.className = b === BROKEN ? "on" : "";
      el.onclick = () => {
        const p = new URLSearchParams(location.search);
        b ? p.set("broken", b) : p.delete("broken");
        location.search = p.toString();
      };
      $("#degraded").append(el);
    });

  await loadTakes();
  $("#pick").innerHTML = S.takes.map((t, i) =>
    `<option value="${i}">${t.take} — ${t.aligned ? "aligné"
      : t.video_file ? "à aligner" : "sans vidéo"}</option>`).join("");
  const first = S.takes.findIndex((t) => t.video_file);
  $("#pick").value = Math.max(0, first);
  await selectTake(S.takes[Math.max(0, first)]);
})();
