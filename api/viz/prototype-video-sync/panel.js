// panel.js — PROTOTYPE #8 : le bloc d'instrument, posé dans l'aside du viz.
//
// Il vit dans le panneau latéral et non sur la scène, parce que la scène est
// précisément ce qu'on juge : un instrument posé dessus fausserait la question
// de mise en page qu'on essaie de trancher.
//
// L'alignement se règle ici à la main. C'est une **béquille assumée** : la
// vraie interface d'alignement est le ticket #9, et ce prototype n'a pas à la
// préfigurer. Il lui faut juste deux ancres plausibles pour que la synchro ait
// quelque chose à suivre.

import { MODES } from "./sync-clock.js";
import { drawDrift } from "./measure.js";

export function mountPanel(aside, ctl) {
  const block = document.createElement("section");
  block.className = "block";
  block.id = "proto-panel";
  block.innerHTML = `
    <h2>Prototype #8 — vidéo</h2>
    <div id="proto-take" class="muted small">—</div>

    <div class="row">
      <label title="Détecté depuis le CSV : premier échantillon qui met fin à
un silence de 2 s sous 0,5 rad/s (décision #7)">ancre IMU
        <input id="proto-imu" type="number" step="0.001"></label>
    </div>
    <div class="row">
      <label title="À régler à la main : aucune machine ne la devine.
Mettre la lecture à l'arrêt, chercher le début du mouvement avec les contrôles
de la vidéo, puis « caler ici ».">ancre vidéo
        <input id="proto-vid" type="number" step="0.001"></label>
    </div>
    <div class="row">
      <button class="mini" data-nudge="-0.5">−500</button>
      <button class="mini" data-nudge="-0.1">−100</button>
      <button class="mini" data-nudge="-0.033">−1 img</button>
      <button class="mini" data-nudge="0.033">+1 img</button>
      <button class="mini" data-nudge="0.1">+100</button>
      <button class="mini" data-nudge="0.5">+500</button>
    </div>
    <div class="row">
      <button id="proto-here" class="mini" title="Prend le mediaTime de l'image
affichée — la seule valeur qui porte le PTS réel (acquis n°10)">caler ici (mediaTime)</button>
      <span id="proto-offset" class="muted small"></span>
    </div>

    <div class="row">
      <label>recalage <select id="proto-mode"></select></label>
    </div>
    <div class="row">
      <label title="Au-delà de cet écart, et seulement au-delà, on écrit
currentTime. L'acquis n°7 avance ~100 ms ; mesuré, c'est le pire endroit — juste
au-dessus du biais naturel, donc franchi sans arrêt. 250 ms : zéro recalage.">seuil
        <input id="proto-thr" type="number" min="10" max="1000" step="10"> ms</label>
    </div>

    <canvas id="proto-chart"></canvas>
    <div class="kv"><span>état</span><span id="proto-state">—</span></div>
    <div class="kv"><span>dérive (commande)</span><span id="proto-drift">—</span></div>
    <div class="kv"><span>dérive (mediaTime)</span><span id="proto-driftm">—</span></div>
    <div class="kv"><span>max | rms</span><span id="proto-stat">—</span></div>
    <div class="kv"><span>passe précédente</span><span id="proto-prev">—</span></div>
    <div class="kv"><span>recalages</span><span id="proto-resync">—</span></div>
    <div class="kv"><span>vitesse dem. / obt.</span><span id="proto-rate">—</span></div>

    <div class="row" style="margin-top:10px">
      <label title="Détache la source du <video> : le décodeur s'arrête, la
mise en page ne bouge pas. C'est l'A/B qui dit ce que la vidéo coûte."><input id="proto-detach" type="checkbox"> vidéo détachée (A/B du coût)</label>
    </div>
    <div class="kv"><span>fps · paq/s — avec vidéo</span><span id="proto-cost-on">—</span></div>
    <div class="kv"><span>fps · paq/s — sans vidéo</span><span id="proto-cost-off">—</span></div>
    <div class="kv"><span>images vidéo perdues</span><span id="proto-dropped">—</span></div>

    <div class="row">
      <button id="proto-copy" class="mini">copier le relevé</button>
      <button id="proto-clear" class="mini">remettre à zéro</button>
    </div>
    <div id="proto-hint" class="muted small"></div>`;
  aside.appendChild(block);

  const $ = (id) => block.querySelector(`#${id}`);
  const modeSel = $("proto-mode");
  for (const [k, v] of Object.entries(MODES)) {
    const o = document.createElement("option");
    o.value = k; o.textContent = v;
    modeSel.appendChild(o);
  }

  const imuIn = $("proto-imu"), vidIn = $("proto-vid");
  const chart = $("proto-chart");
  let lastSummary = null;   // la passe précédente, pour le relevé

  const pushAlignment = () =>
    ctl.onAlignment(parseFloat(imuIn.value), parseFloat(vidIn.value));

  imuIn.onchange = vidIn.onchange = pushAlignment;
  block.querySelectorAll("[data-nudge]").forEach((b) => {
    b.onclick = () => {
      const v = (parseFloat(vidIn.value) || 0) + parseFloat(b.dataset.nudge);
      vidIn.value = v.toFixed(3);
      pushAlignment();
    };
  });
  $("proto-here").onclick = () => {
    const m = ctl.lastMediaTime();
    if (!Number.isFinite(m)) return;
    vidIn.value = m.toFixed(3);
    pushAlignment();
  };
  modeSel.onchange  = () => ctl.onMode(modeSel.value);
  $("proto-thr").onchange = (e) => ctl.onThreshold(Math.max(10, +e.target.value) / 1000);
  $("proto-detach").onchange = (e) => ctl.onDetach(e.target.checked);
  $("proto-copy").onclick  = () => ctl.onCopy();
  $("proto-clear").onclick = () => ctl.onClear();

  const fmtMs = (s) => (Number.isFinite(s) ? `${(s * 1000).toFixed(0)} ms` : "—");
  const fmtCost = (c) =>
    c.n ? `${c.fps} fps · ${c.pps} Hz  (n=${c.n})` : "—";

  return {
    /**
     * Les deux réglages de recalage sont détenus par l'horloge, pas par ce
     * bloc : les contrôles se remplissent depuis elle au montage, plutôt que
     * de répéter ses valeurs par défaut dans le HTML — deux copies d'un même
     * défaut, c'est une divergence qui attend son heure, et un panneau qui
     * affiche un réglage que le code n'applique pas ment sur le seul point que
     * ce prototype est là pour mesurer.
     */
    syncControls(clock) {
      modeSel.value = clock.mode;
      $("proto-thr").value = Math.round(clock.thresholdS * 1000);
    },

    /** Le take suivi, et l'ancre IMU que le backend propose pour lui. */
    setTake(info, align) {
      $("proto-take").textContent = info
        ? `${info.take} — ${info.video_file}`
        : "aucune vidéo pour ce take";
      $("proto-take").className = info ? "small" : "muted small";
      imuIn.value = Number.isFinite(align.imu) ? align.imu.toFixed(3) : "";
      vidIn.value = Number.isFinite(align.video) ? align.video.toFixed(3) : "";
    },

    setHint(text) { $("proto-hint").textContent = text; },

    /** Le résumé de la passe qui vient de se terminer, avant que l'anneau parte. */
    setPrevious(s) {
      lastSummary = s;
      $("proto-prev").textContent =
        `${s.drift_max_ms} | ${s.drift_rms_ms} ms · ${s.recalages} rec. · ${s.duree_s} s`;
    },

    update(clock, rec, cost) {
      const st = clock.stats;
      $("proto-state").textContent = st.state;
      $("proto-state").className =
        st.state === "suit le replay" ? "ok" : st.outOfRange ? "bad" : "warn";
      $("proto-drift").textContent  = fmtMs(st.drift);
      $("proto-driftm").textContent = fmtMs(st.driftMedia);

      const s = rec.summary();
      $("proto-stat").textContent =
        s.samples ? `${s.drift_max_ms} | ${s.drift_rms_ms} ms` : "—";
      $("proto-resync").textContent =
        st.resyncs + (st.lastResync ? `  (dernier : ${st.lastResync.cause})` : "");
      $("proto-rate").textContent =
        `×${st.rateAsked.toFixed(3)} / ×${st.rateGot.toFixed(3)}` +
        (Math.abs(st.rateAsked - st.rateGot) > 1e-2 ? "  ⚠ refusée" : "");

      $("proto-offset").textContent =
        Number.isFinite(clock.onsetImuS) && Number.isFinite(clock.onsetVideoS)
          ? `décalage ${(clock.onsetVideoS - clock.onsetImuS).toFixed(3)} s`
          : "non aligné";

      $("proto-cost-on").textContent  = fmtCost(cost.stats("on"));
      $("proto-cost-off").textContent = fmtCost(cost.stats("off"));
      const d = cost.dropped();
      $("proto-dropped").textContent = d ? `${d.dropped} / ${d.total}  (${d.pct} %)` : "—";

      drawDrift(chart, rec, clock.thresholdS);
    },

    previous: () => lastSummary,

    remove() { block.remove(); },
  };
}
