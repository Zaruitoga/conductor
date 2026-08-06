// PROTOTYPE JETABLE (#9) — trois mises en page de l'interface d'alignement.
//
//   ?variant=A   « Table de montage »  — la vidéo occupe tout, la courbe est un ruban
//   ?variant=B   « Deux ancres »       — 50/50, les deux ancres sont des objets égaux
//   ?variant=C   « Assistant »         — une bande d'étapes, et la roue 3D pour voir si
//                                        elle mérite sa place
//
//   &broken=onset        la détection ne propose rien
//   &broken=unreadable   le fichier vidéo ne se décode pas
//   (le take 001 n'a réellement pas de vidéo : c'est le troisième état dégradé)
//
// Les variantes ne partagent que `engine.js`. Chacune écrit son propre DOM et sa
// propre carte de touches — c'est ce qui est en jeu.

import { api, Stepper, drawCurve, drawWheel, fmt } from "./engine.js";

const qp = new URLSearchParams(location.search);
const VARIANT = (qp.get("variant") || "A").toUpperCase();
const BROKEN = qp.get("broken") || "";

const S = {
  takes: [], cur: null, data: null, stepper: null, video: null,
  imu: null,          // ancre IMU retenue (proposée puis éventuellement corrigée)
  proposal: null,     // ce que la détection a proposé, jamais écrasé
  vid: null,          // ancre vidéo = mediaTime MESURÉ (#10)
  playing: false, step: 1,
};

const app = document.getElementById("app");
const $ = (s) => app.querySelector(s);
const key = (t) => `${t.session}/${t.take}`;

// ── Données ─────────────────────────────────────────────────────────────────
async function loadTakes() {
  S.takes = await api("/api/takes");
}

async function selectTake(t) {
  S.cur = t; S.vid = null; S.playing = false; S.step = 1;
  const q = `session=${encodeURIComponent(t.session)}&take=${encodeURIComponent(t.take)}`;
  S.data = await api(`/api/onset?${q}&broken=${BROKEN}`);
  S.proposal = S.data.onset_imu_s;
  S.imu = S.proposal;
  V.onTake();
  if (!t.video_file && BROKEN !== "unreadable") { V.refresh(); return; }
  S.video.src = `/api/video?${q}&broken=${BROKEN}`;
  S.stepper = new Stepper(S.video).on(() => V.refresh());
  S.video.addEventListener("loadeddata", async () => {
    await S.stepper.measure();
    V.refresh();
  }, { once: true });
  S.video.addEventListener("error", () => { S.stepper = null; V.refresh(); }, { once: true });
  V.refresh();
}

async function confirm() {
  if (S.imu == null || S.vid == null) return;
  await api("/api/align", {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ session: S.cur.session, take: S.cur.take,
                           onset_imu_s: S.imu, onset_video_s: S.vid }),
  });
  await loadTakes();
  S.cur = S.takes.find((t) => key(t) === key(S.cur));
  V.refresh();
}

// Après confirmation : la vidéo rejoue et le curseur court sur la courbe.
function playAligned() {
  if (!S.video || !S.cur?.aligned) return;
  S.playing = !S.playing;
  if (!S.playing) { S.video.pause(); V.refresh(); return; }
  S.video.play();
  const tick = () => {
    if (!S.playing || S.video.paused) { S.playing = false; V.refresh(); return; }
    V.refresh();
    requestAnimationFrame(tick);
  };
  tick();
}

const mappedImu = () => (S.cur?.aligned && S.video
  ? S.video.currentTime - S.cur.aligned.onset_video_s + S.cur.aligned.onset_imu_s : null);

// Échantillon gyro le plus proche — le curseur IMU se déplace d'échantillon en
// échantillon, pas en temps continu : c'est la vraie granularité de la donnée.
function imuNudge(n) {
  const c = S.data.curve;
  let i = 0;
  while (i < c.length - 1 && c[i][0] < S.imu) i++;
  S.imu = c[Math.max(0, Math.min(c.length - 1, i + n))][0];
  V.refresh();
}

function quatAt(t) {
  const q = S.data?.quats;
  if (!q?.length) return null;
  let i = 0;
  while (i < q.length - 1 && q[i][0] < t) i++;
  return q[i].slice(1);
}

// ── États dégradés, rendus par toutes les variantes ─────────────────────────
function stageFallback() {
  if (!S.cur) return `<div class="fallback">…</div>`;
  if (!S.cur.video_file && BROKEN !== "unreadable")
    return `<div class="fallback"><b>Pas de vidéo pour ce take.</b>
      <span>Copier le fichier dans <code>sessions/${S.cur.session}/takes/${S.cur.take}/</code>,
      puis rafraîchir. L'ancre IMU est proposée quand même : elle n'attend pas la vidéo.</span></div>`;
  if (S.stepper === null && S.video?.error)
    return `<div class="fallback bad"><b>Le fichier ne se décode pas.</b>
      <span>${S.cur.video_file} — le navigateur refuse le flux. Rien à aligner tant qu'il
      n'est pas lisible.</span></div>`;
  return null;
}

const badge = (t) => t.aligned
  ? `<span class="pill ok">aligné · Δ ${(t.aligned.onset_video_s - t.aligned.onset_imu_s).toFixed(3)} s</span>`
  : t.video_file ? `<span class="pill">à aligner</span>` : `<span class="pill dim">sans vidéo</span>`;

function readout() {
  const st = S.stepper;
  if (!st) return "—";
  // Si rVFC ne rappelle pas, tout ce qui suit est un repli sur `currentTime` —
  // que #4 déclare non fiable pour dire quelle frame est affichée. On le dit en
  // bandeau global plutôt que d'en gonfler la ligne de lecture.
  if (st.rvfc === false) {
    const b = document.getElementById("banner");
    b.textContent = "⚠ requestVideoFrameCallback ne rappelle pas dans ce navigateur : repli sur "
      + "currentTime, cadence supposée. Ouvrir dans Chrome ou Safari pour juger le geste.";
    b.hidden = false; document.body.classList.add("warned");
    document.documentElement.style.setProperty("--banner", b.offsetHeight + "px");
  }
  return `<b>${st.media.toFixed(3)} s</b><span class="dim"> · frame ≈ ${st.frameApprox}
    · cadence ${st.rvfc === false ? "supposée" : "mesurée"} ${(1 / st.dt).toFixed(2)} fps${st.spread
      ? ` (${(st.spread[0] * 1000).toFixed(1)}–${(st.spread[1] * 1000).toFixed(1)} ms)` : ""}
    · ${st.tries} aller-retour${st.tries > 1 ? "s" : ""}</span>`;
}

function curveMarkers() {
  return [
    { t: S.proposal, color: "#8a939c", dashed: true, label: "proposition" },
    { t: S.imu, color: "#3fb950", wide: true, label: `ancre IMU ${fmt(S.imu)}` },
    { t: mappedImu(), color: "#f85149", label: "lecture" },
  ];
}

const noOnset = () => S.data && S.data.onset_imu_s == null;

// ═══ VARIANTE A — « Table de montage » ══════════════════════════════════════
// Hypothèse : la vidéo EST le travail ; la courbe est un ruban qu'on regarde du
// coin de l'œil. Un seul geste, aucune étape.
const A = {
  name: "Table de montage",
  keys: "← → frame · ⇧← ⇧→ 10 frames · ↑ ↓ 1 s · Espace lecture · Entrée confirmer · clic sur le ruban = ancre IMU",
  mount() {
    app.innerHTML = `
      <div class="A">
        <header>
          <select id="pick"></select>
          <span id="state"></span>
          <span class="dim keys">${A.keys}</span>
        </header>
        <main id="stage"><video id="v" playsinline></video>
          <div id="ro" class="readout"></div></main>
        <footer>
          <canvas id="ribbon"></canvas>
          <div class="bar">
            <span id="anchors"></span>
            <button id="ok">Confirmer l'alignement</button>
            <button id="play" class="ghost">Relire aligné</button>
          </div>
        </footer>
      </div>`;
    S.video = $("#v");
    $("#pick").onchange = (e) => selectTake(S.takes[e.target.value]);
    $("#ok").onclick = () => { S.vid = S.stepper?.media ?? null; confirm(); };
    $("#play").onclick = playAligned;
    $("#ribbon").onclick = (e) => {
      const r = e.target.getBoundingClientRect();
      S.imu = (e.clientX - r.left) / r.width * S.data.duration_s; V.refresh();
    };
    addEventListener("keydown", (e) => {
      if (e.altKey || e.metaKey || e.ctrlKey) return;
      const k = e.key;
      if (k === "ArrowRight" || k === "ArrowLeft") {
        e.preventDefault(); S.stepper?.step((k === "ArrowRight" ? 1 : -1) * (e.shiftKey ? 10 : 1));
      } else if (k === "ArrowUp" || k === "ArrowDown") {
        e.preventDefault(); S.stepper?.seek(S.stepper.media + (k === "ArrowUp" ? 1 : -1));
      } else if (k === " ") { e.preventDefault(); playAligned(); }
      else if (k === "Enter") { e.preventDefault(); S.vid = S.stepper?.media ?? null; confirm(); }
    });
  },
  onTake() {
    $("#pick").innerHTML = S.takes.map((t, i) =>
      `<option value="${i}" ${key(t) === key(S.cur) ? "selected" : ""}>${t.take} — ${
        t.aligned ? "aligné" : t.video_file ? "à aligner" : "sans vidéo"}</option>`).join("");
  },
  refresh() {
    const fb = stageFallback();
    $("#stage").classList.toggle("empty", !!fb);
    $("#v").style.display = fb ? "none" : "";
    $("#ro").innerHTML = fb ?? readout();
    $("#state").innerHTML = S.cur ? badge(S.cur) : "";
    $("#anchors").innerHTML = `IMU <b>${fmt(S.imu)}</b> ${
      noOnset() ? `<span class="warn">rien de détecté — cliquer le ruban</span>` : ""} ·
      vidéo <b>${S.stepper ? S.stepper.media.toFixed(3) + " s" : "—"}</b>`;
    $("#ok").disabled = !S.stepper || S.imu == null;
    $("#play").disabled = !S.cur?.aligned;
    if (S.data) drawCurve($("#ribbon"), {
      curve: S.data.curve, t0: 0, t1: S.data.duration_s,
      thr: S.data.silence_rad_s, markers: curveMarkers(),
    });
  },
};

// ═══ VARIANTE B — « Deux ancres » ═══════════════════════════════════════════
// Hypothèse : vérifier la proposition IMU est une vraie étape, pas un coup d'œil.
// La courbe mérite la moitié de l'écran, et la corriger doit se faire sur place.
const B = {
  name: "Deux ancres",
  keys: "← → frame · ⇧← ⇧→ 1 s · ↑ ↓ échantillon IMU · A poser l'ancre vidéo · + − zoom · Entrée confirmer",
  zoom: 2,
  mount() {
    app.innerHTML = `
      <div class="B">
        <aside id="rail"></aside>
        <section class="left">
          <video id="v" playsinline></video>
          <div id="ro" class="readout"></div>
        </section>
        <section class="right">
          <div class="row"><h2>Courbe |ω| brute</h2>
            <span class="dim" id="zoomlbl"></span></div>
          <canvas id="curve"></canvas>
          <div class="anchors">
            <div class="card" id="cimu"></div>
            <div class="card" id="cvid"></div>
          </div>
          <div class="row">
            <button id="ok">Confirmer</button>
            <button id="play" class="ghost">Relire aligné</button>
            <span class="dim keys">${B.keys}</span>
          </div>
        </section>
      </div>`;
    S.video = $("#v");
    $("#ok").onclick = () => confirm();
    $("#play").onclick = playAligned;
    $("#curve").onclick = (e) => {
      const r = e.target.getBoundingClientRect();
      const w = B.window();
      S.imu = w[0] + (e.clientX - r.left) / r.width * (w[1] - w[0]); V.refresh();
    };
    addEventListener("keydown", (e) => {
      if (e.altKey || e.metaKey || e.ctrlKey) return;
      const k = e.key;
      if (k === "ArrowRight" || k === "ArrowLeft") {
        e.preventDefault();
        const d = k === "ArrowRight" ? 1 : -1;
        e.shiftKey ? S.stepper?.seek(S.stepper.media + d) : S.stepper?.step(d);
      } else if (k === "ArrowUp" || k === "ArrowDown") {
        e.preventDefault(); imuNudge(k === "ArrowUp" ? 1 : -1);
      } else if (k === "a" || k === "A") {
        e.preventDefault(); S.vid = S.stepper?.media ?? null; V.refresh();
      } else if (k === "+" || k === "=") { B.zoom = Math.max(0.1, B.zoom / 1.6); V.refresh(); }
      else if (k === "-") { B.zoom = Math.min(120, B.zoom * 1.6); V.refresh(); }
      else if (k === "Enter") { e.preventDefault(); confirm(); }
    });
  },
  window() {
    const c = S.imu ?? (S.data?.duration_s ?? 10) / 2;
    return [c - B.zoom, c + B.zoom];
  },
  onTake() {
    $("#rail").innerHTML = S.takes.map((t, i) =>
      `<button class="railitem ${key(t) === key(S.cur) ? "on" : ""}" data-i="${i}">
         <b>${t.take}</b>${badge(t)}</button>`).join("");
    $("#rail").querySelectorAll("[data-i]").forEach((b) =>
      b.onclick = () => selectTake(S.takes[b.dataset.i]));
  },
  refresh() {
    const fb = stageFallback();
    $("#v").style.display = fb ? "none" : "";
    $("#ro").innerHTML = fb ?? readout();
    $("#zoomlbl").textContent = `fenêtre ±${B.zoom.toFixed(2)} s`;
    $("#cimu").innerHTML = `<h3>Ancre IMU</h3><div class="big">${fmt(S.imu)}</div>
      <div class="dim">${noOnset() ? `<span class="warn">aucune proposition — la placer à la main
        (↑↓ ou clic)</span>`
        : S.imu === S.proposal ? `proposée par la détection · silence &lt; ${S.data?.silence_rad_s}
          rad/s pendant ${S.data?.min_silence_s} s`
        : `corrigée à la main · proposition ${fmt(S.proposal)}`}</div>`;
    $("#cvid").innerHTML = `<h3>Ancre vidéo</h3><div class="big">${fmt(S.vid)}</div>
      <div class="dim">${S.vid == null ? "touche <b>A</b> pour la poser sur la frame affichée"
        : S.stepper?.rvfc === false ? `<span class="warn">repli currentTime — pas un mediaTime
          mesuré</span>` : "mediaTime mesuré, pas un n/fps calculé"}</div>`;
    $("#ok").disabled = S.imu == null || S.vid == null;
    $("#play").disabled = !S.cur?.aligned;
    $("#ok").textContent = S.imu != null && S.vid != null
      ? `Confirmer · Δ ${(S.vid - S.imu).toFixed(3)} s` : "Confirmer";
    if (S.data) {
      const [t0, t1] = B.window();
      drawCurve($("#curve"), { curve: S.data.curve, t0, t1,
        thr: S.data.silence_rad_s, markers: curveMarkers() });
    }
    B.onTake();
  },
};

// ═══ VARIANTE C — « Assistant » ═════════════════════════════════════════════
// Hypothèse : la page a un ordre et devrait le dire. Et la roue 3D est ici pour
// qu'on tranche en la voyant, pas en l'imaginant.
const STEPS = ["Vérifier l'ancre IMU", "Placer la frame vidéo", "Confirmer"];
const C = {
  name: "Assistant + roue",
  keys: "← → selon l'étape · ⇧ ×10 · Entrée étape suivante · ⌫ revenir",
  mount() {
    app.innerHTML = `
      <div class="C">
        <nav id="chips"></nav>
        <div class="mid">
          <section class="vid"><video id="v" playsinline></video>
            <div id="ro" class="readout"></div></section>
          <section class="wheel"><canvas id="wheel"></canvas>
            <div class="dim center" id="wlbl"></div></section>
        </div>
        <canvas id="curve"></canvas>
        <footer id="steps"></footer>
      </div>`;
    S.video = $("#v");
    addEventListener("keydown", (e) => {
      if (e.altKey || e.metaKey || e.ctrlKey) return;
      const k = e.key, m = e.shiftKey ? 10 : 1;
      if (k === "ArrowRight" || k === "ArrowLeft") {
        e.preventDefault();
        const d = (k === "ArrowRight" ? 1 : -1) * m;
        S.step === 1 ? imuNudge(d) : S.stepper?.step(d);
      } else if (k === "Enter") {
        e.preventDefault();
        if (S.step === 3) { confirm(); return; }
        if (S.step === 2) S.vid = S.stepper?.media ?? null;
        S.step++; V.refresh();
      } else if (k === "Backspace") {
        e.preventDefault(); S.step = Math.max(1, S.step - 1); V.refresh();
      }
    });
  },
  onTake() {
    $("#chips").innerHTML = S.takes.map((t, i) =>
      `<button class="chip ${key(t) === key(S.cur) ? "on" : ""}" data-i="${i}">${t.take}
        ${t.aligned ? "✓" : t.video_file ? "" : "∅"}</button>`).join("");
    $("#chips").querySelectorAll("[data-i]").forEach((b) =>
      b.onclick = () => selectTake(S.takes[b.dataset.i]));
  },
  refresh() {
    const fb = stageFallback();
    $("#v").style.display = fb ? "none" : "";
    $("#ro").innerHTML = fb ?? readout();
    $("#steps").innerHTML = STEPS.map((s, i) => `
      <div class="step ${S.step === i + 1 ? "on" : S.step > i + 1 ? "done" : ""}">
        <b>${i + 1}. ${s}</b>
        <span class="dim">${[
          noOnset() ? "aucune proposition — la placer avec ← →"
            : `proposée à ${fmt(S.proposal)}${S.imu !== S.proposal ? ` → corrigée ${fmt(S.imu)}` : ""}`,
          S.stepper ? `frame ≈ ${S.stepper.frameApprox} · ${S.stepper.media.toFixed(3)} s` : "—",
          S.imu != null && S.vid != null ? `Δ ${(S.vid - S.imu).toFixed(3)} s — Entrée`
            : "il manque une ancre",
        ][i]}</span>
      </div>`).join("") + `<span class="dim keys">${C.keys}</span>`;
    if (S.data) {
      drawCurve($("#curve"), { curve: S.data.curve,
        t0: (S.imu ?? 0) - 1.2, t1: (S.imu ?? 0) + 1.2,
        thr: S.data.silence_rad_s, markers: curveMarkers() });
      drawWheel($("#wheel"), quatAt(S.imu ?? 0));
      $("#wlbl").textContent = `attitude à l'ancre IMU (${fmt(S.imu)})`;
    }
    C.onTake();
  },
};

// ── Commutateur ─────────────────────────────────────────────────────────────
// Volontairement PAS sur ← → : ces touches sont exactement l'objet du test.
const VARIANTS = { A, B, C };
const V = VARIANTS[VARIANT] ?? A;

function switcher() {
  const bar = document.getElementById("switch");
  bar.innerHTML = Object.entries(VARIANTS).map(([k, v]) =>
    `<button data-v="${k}" class="${k === VARIANT ? "on" : ""}">${k} — ${v.name}</button>`).join("")
    + `<span class="sep"></span>` + [["", "nominal"], ["onset", "sans proposition"],
       ["unreadable", "vidéo illisible"]].map(([b, l]) =>
      `<button data-b="${b}" class="${b === BROKEN ? "on" : ""}">${l}</button>`).join("");
  bar.onclick = (e) => {
    const b = e.target.closest("button"); if (!b) return;
    const p = new URLSearchParams(location.search);
    if (b.dataset.v) p.set("variant", b.dataset.v);
    if (b.dataset.b != null) b.dataset.b ? p.set("broken", b.dataset.b) : p.delete("broken");
    location.search = p.toString();
  };
}

(async function boot() {
  switcher();
  V.mount();
  await loadTakes();
  await selectTake(S.takes.find((t) => t.video_file) ?? S.takes[0]);
  addEventListener("resize", () => V.refresh());
})();
