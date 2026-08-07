// mount.js — PROTOTYPE #8 : le raccordement.
//
// Tout ce qui est jetable est ici : créer le <video>, choisir la variante,
// brancher l'horloge de synchro sur les deux canaux que le viz reçoit déjà, et
// tenir l'instrument à jour. Le seul module qui mérite d'être relevé le jour de
// l'implémentation est `sync-clock.js` ; celui-ci est un échafaudage.
//
// `?variant=off` éteint le prototype entier : le viz redevient exactement
// lui-même, ce qui est aussi la référence dont l'A/B du coût a besoin.

import { VideoSyncClock } from "./sync-clock.js";
import { DriftRecorder, CostMeter } from "./measure.js";
import { mountPanel } from "./panel.js";
import { mountSwitcher } from "./switcher.js";
import * as variantA from "./variant-a.js";
import * as variantB from "./variant-b.js";
import * as variantC from "./variant-c.js";

const VARIANTS = { A: variantA, B: variantB, C: variantC };
const ALIGN_KEY = (s, t) => `proto8.align.${s}/${t}`;
const NOOP_HOOKS = { onFrame() {}, onMeta() {}, onPlayback() {}, onRates() {} };

export function createVideoSyncPrototype({ three, follow }) {
  const params  = new URLSearchParams(location.search);
  const asked   = (params.get("variant") || "A").toUpperCase();
  if (asked === "OFF") return NOOP_HOOKS;
  const startKey = VARIANTS[asked] ? asked : "A";

  document.head.appendChild(Object.assign(document.createElement("link"), {
    rel: "stylesheet", href: "prototype-video-sync/proto.css",
  }));

  const stage      = document.getElementById("stage");
  const canvasWrap = document.getElementById("canvas-container");
  const hud        = document.getElementById("hud");
  canvasWrap.appendChild(hud);   // le HUD suit la 3D, pas la scène entière

  // ── Le <video> ─────────────────────────────────────────────────────────────
  const videoWrap = document.createElement("div");
  videoWrap.className = "proto-video-wrap";
  const videoBar = document.createElement("div");
  videoBar.className = "proto-video-bar";
  // Le badge d'état vit sur la **scène**, pas seulement dans l'instrument : le
  // panneau est un échafaudage qui ne survivra pas au prototype, et c'est
  // pendant la lecture, l'œil sur l'image, qu'on a besoin de savoir qu'elle ne
  // suit rien. Il reste vide tant que la vidéo suit — `:empty` l'efface.
  videoBar.innerHTML = `<span class="grow">aucune vidéo</span><span class="proto-state"></span>`;
  const video = document.createElement("video");
  // `muted` n'est pas un choix de confort : sans lui, `play()` est refusé sans
  // geste de l'utilisateur. Le son ne fait pas partie de la question posée.
  video.muted = true;
  video.playsInline = true;
  video.preload = "auto";
  // Contrôles natifs laissés en place : c'est ce qui permet de chercher le
  // début du mouvement à la main pour poser l'ancre vidéo, sans écrire une
  // interface d'alignement (ticket #9).
  video.controls = true;
  videoWrap.append(videoBar, video);
  stage.appendChild(videoWrap);
  const caption = videoBar.querySelector(".grow");

  // ── L'horloge, l'anneau, le coût ───────────────────────────────────────────
  // Le défaut n'est plus celui de l'acquis n°7 (recalage dur seul à 100 ms) mais
  // celui que la campagne a désigné : trim + recalage dur à 250 ms. Le prototype
  // s'ouvre donc sur sa propre conclusion, et le réglage de départ reste à un
  // menu de distance pour refaire la comparaison. Chiffres dans le README.
  const clock = new VideoSyncClock(video, { thresholdS: 0.25, mode: "both" });
  const rec   = new DriftRecorder();
  const cost  = new CostMeter();

  let catalog     = new Map();     // "session/take" → info
  let currentKey  = null;
  let lastMediaTime = NaN;
  let lastResyncs = 0;
  let detached    = false;
  let detachedSrc = "";

  const panel = mountPanel(document.getElementById("side"), {
    onAlignment(imu, vid) {
      clock.setAlignment(imu, vid);
      if (currentKey) {
        const [s, t] = currentKey.split("/");
        try {
          localStorage.setItem(ALIGN_KEY(s, t), JSON.stringify({ imu, video: vid }));
        } catch { /* mode privé */ }
      }
    },
    onMode(m)       { clock.setMode(m); },
    onThreshold(s)  { clock.setThreshold(s); },
    onDetach(on)    { setDetached(on); },
    onClear()       { rec.clear(); cost.clear(); clock.stats.resyncs = 0; lastResyncs = 0; },
    onCopy()        { copyReport(); },
    lastMediaTime: () => lastMediaTime,
  });
  panel.syncControls(clock);

  // ── Les variantes ──────────────────────────────────────────────────────────
  const ctx = { stage, canvasWrap, videoWrap, videoBar, three, follow, onLayout() {} };
  let teardown = null;

  function show(key) {
    if (teardown) teardown();
    const v = VARIANTS[key];
    teardown = v.mount(ctx);
    panel.setHint(v.hint);
    return v.name;
  }

  const switcher = mountSwitcher(Object.keys(VARIANTS), startKey, show);
  switcher.setLabel(show(startKey));

  // ── Le catalogue des takes filmés ──────────────────────────────────────────
  fetch("/api/prototype/video-sync/takes")
    .then((r) => r.json())
    .then((d) => {
      catalog = new Map(d.takes.map((t) => [`${t.session}/${t.take}`, t]));
      // Précharger dès qu'on choisit un take dans le sélecteur du viz, sans
      // attendre « Lire » : sinon la première seconde de lecture part à vide.
      // Un sondage plutôt qu'un écouteur `change` : le viz repeuple ses <select>
      // par code (`refreshSessions`), ce qui ne déclenche aucun événement.
      // `load()` sort tout de suite quand la clé n'a pas bougé — c'est gratuit.
      const sel = document.getElementById("pb-take");
      const ses = document.getElementById("pb-session");
      setInterval(() => {
        if (clock.active) return;   // en lecture, c'est le replay qui décide
        if (ses.value && sel.value) load(`${ses.value}/${sel.value}`);
      }, 1000);
    })
    .catch(() => { caption.textContent = "catalogue vidéo indisponible"; });

  function load(key) {
    if (key === currentKey) return;
    currentKey = key;
    const info = catalog.get(key) || null;
    const [s, t] = key.split("/");

    let saved = {};
    try { saved = JSON.parse(localStorage.getItem(ALIGN_KEY(s, t))) || {}; } catch { /* … */ }
    const align = {
      imu:   Number.isFinite(saved.imu)   ? saved.imu   : (info ? info.imu_onset_s : NaN),
      video: Number.isFinite(saved.video) ? saved.video : NaN,
    };

    clock.setAlignment(align.imu, align.video);
    panel.setTake(info, align);
    rec.clear();

    caption.textContent = info ? `${info.take} — ${info.video_file}` : "aucune vidéo";
    videoWrap.classList.toggle("proto-no-video", !info);
    if (info && !detached) { video.src = info.video_url; video.load(); }
    else if (!info) { video.removeAttribute("src"); video.load(); }
    detachedSrc = info ? info.video_url : "";
  }

  function setDetached(on) {
    detached = on;
    if (on) { video.removeAttribute("src"); video.load(); }
    else if (detachedSrc) { video.src = detachedSrc; video.load(); }
  }

  // ── La boucle d'images présentées (mesure de la dérive vraie) ──────────────
  if (video.requestVideoFrameCallback) {
    const onPresented = (_now, meta) => {
      lastMediaTime = meta.mediaTime;
      clock.onPresentedFrame(meta.mediaTime);
      video.requestVideoFrameCallback(onPresented);
    };
    video.requestVideoFrameCallback(onPresented);
  } else {
    panel.setHint("requestVideoFrameCallback absent — dérive mesurée sur currentTime seul");
  }

  const stateBadge = videoBar.querySelector(".proto-state");
  setInterval(() => {
    panel.update(clock, rec, cost);
    // « suit le replay » est le cas nominal : on ne l'affiche pas, un badge
    // permanent cesse d'être lu au bout d'une minute. Hors lecture non plus —
    // une vignette immobile pendant qu'il ne se passe rien n'étonne personne.
    // Un take sans vidéo n'est pas « non aligné » : il n'y a rien à aligner, et
    // le bandeau le dit déjà. Deux façons de nommer la même absence en valent
    // une de trop.
    const noVideo = videoWrap.classList.contains("proto-no-video");
    const off = clock.active && !noVideo && clock.stats.state !== "suit le replay";
    stateBadge.textContent = off ? clock.stats.state : "";
    videoWrap.classList.toggle("proto-idle", off);
  }, 200);

  // Les chiffres du ticket ont été relevés d'ici : une campagne (quatre
  // réglages × une passe chacun) se pilote depuis la console, pas à la souris,
  // sinon la comparaison porte autant sur l'opérateur que sur le réglage.
  // Ce n'est pas une API — c'est l'établi ouvert, comme le reste du prototype.
  window.__proto8 = { clock, rec, cost, panel, load };

  function copyReport() {
    const info = catalog.get(currentKey);
    const report = {
      ticket:      "#8",
      take:        currentKey,
      video:       info ? info.video_file : null,
      onset_imu_s: clock.onsetImuS,
      onset_video_s: clock.onsetVideoS,
      mode:        clock.mode,
      seuil_ms:    clock.thresholdS * 1000,
      vitesse:     { demandee: clock.stats.rateAsked, obtenue: clock.stats.rateGot },
      derive:      rec.n > 200 ? rec.summary() : panel.previous(),
      cout:        { avec: cost.stats("on"), sans: cost.stats("off"), images_perdues: cost.dropped() },
      navigateur:  navigator.userAgent,
    };
    navigator.clipboard.writeText(JSON.stringify(report, null, 2))
      .then(() => panel.setHint("relevé copié dans le presse-papier"))
      .catch(() => console.log(report));
  }

  // ── Les crochets que viz.js appelle ────────────────────────────────────────
  return {
    onFrame(d) {
      clock.onFrame(d.t);
      if (!clock.active || clock.paused) return;

      const resynced = clock.stats.resyncs !== lastResyncs;
      const cause    = resynced ? clock.stats.lastResync.cause : null;

      // Un recalage dur (reset de boucle, reprise après pause) mesure une
      // erreur d'avant correction, qui n'est bornée par rien : au tour de
      // boucle, la vidéo est encore à sa fin pendant que la timeline repart de
      // zéro, soit 50 s d'« écart ». L'enregistrer écraserait l'échelle et
      // rendrait invisible le signal de ±100 ms qu'on cherche à lire. Le
      // recalage est marqué, sa valeur ne l'est pas. Un franchissement de seuil,
      // lui, mesure bien ce qu'on veut : il vaut à peu près le seuil.
      if (Number.isFinite(clock.stats.drift) && (!resynced || cause === "seuil")) {
        rec.push(d.t, clock.stats.drift, clock.stats.driftMedia);
      }
      if (resynced) {
        lastResyncs = clock.stats.resyncs;
        rec.mark(d.t, cause);
      }
    },

    onMeta(m) {
      if (m.topic !== "reset") return;
      clock.onReset();
      // Le reset arrive aussi bien au début d'un tour de boucle qu'à la fin
      // d'une passe. Vider l'anneau est correct — les horodatages repartent à
      // zéro — mais effacerait la mesure au moment précis où elle vient de se
      // terminer. On en garde donc le résumé avant de vider.
      if (rec.n > 200) panel.setPrevious(rec.summary());
      rec.clear();
    },

    onPlayback(p) {
      if (p && p.active && p.session && p.take) load(`${p.session}/${p.take}`);
      clock.onPlayback(p);
    },

    onRates({ fps, pps }) {
      // Uniquement pendant une lecture : une seconde d'inactivité produit
      // 0 fps et 0 paquet/s, et une médiane prise sur du repos ne compare
      // rien du tout. C'est l'écart *sous charge* qui est la question.
      if (!clock.active || clock.paused) return;
      const q = video.getVideoPlaybackQuality ? video.getVideoPlaybackQuality() : null;
      cost.sample(!detached && !!video.currentSrc, fps, pps,
                  q ? q.droppedVideoFrames : 0, q ? q.totalVideoFrames : 0);
    },
  };
}
