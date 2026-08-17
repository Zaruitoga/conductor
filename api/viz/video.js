// video.js — the take's video, in the scene, slave to the replay.
//
// Everything about *how it follows* lives in `sync-clock.js`, and everything
// about *what the scene shows of it* in `layout.js`; this file is the wiring:
// one `<video>` on the scene, the file it plays, the permanent
// `requestVideoFrameCallback` chain, and the appliance of what `describe()`
// decided — the frame's place, the hatching, the badge, the greying.
//
// It decides none of that itself. The same description drives `viz.js` (the
// camera follow and the renderer's transparency), and two appliers agreeing by
// hand would agree right up to the day one of them changed.
//
// Muted by default, with an explicit toggle (decision #19). Unmuted it would
// demand a user gesture before starting, which breaks the one thing this page is
// for — a replay started from here and left running. Muted, the browser pauses
// it in a hidden tab, which is exactly what the hard resync covers. The toggle
// to sound *is* the gesture that unblocks audio, so nothing is lost.

import { VideoSyncClock } from "./sync-clock.js";
import { describe, DEFAULT_LAYOUT } from "./layout.js";

// A `loadeddata` fired in a hidden tab presents nothing, so the domain offset
// cannot be measured there. It is retried when the page comes back rather than
// treated as measured — a missing measurement is not a measurement of zero.
const REMEASURE_MS = 300;

const el = (tag, cls) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  return n;
};

const enc = encodeURIComponent;

/**
 * Mount the video on the scene.
 *
 * @param {HTMLElement} stage  the scene container (`#stage`)
 * @param {object} [opts]
 * @param {(view: object) => void} [opts.onView]  called when the description of
 *   the scene's own share changes — the camera follow and the renderer's
 *   transparency, which belong to Three.js and not here.
 * @returns hooks the page calls where the information already passes
 */
export function mountVideo(stage, { onView } = {}) {
  const wrap  = el("div", "video-wrap");
  const bar   = el("div", "video-bar");
  const label = el("span", "grow");
  const drift = el("span", "video-drift");
  const state = el("span", "video-chip");
  const swap  = el("button", "video-btn");
  const sound = el("button", "video-btn");

  const video = el("video");
  video.muted       = true;
  video.playsInline = true;
  video.preload     = "auto";
  // No native controls: the replay drives this element, and a hand on a
  // scrubber would be a second driver on the same `currentTime`. Designating a
  // frame is `/align/`'s job, on its own page.

  sound.textContent = "🔇";
  sound.title = "Activer le son";
  sound.onclick = () => {
    video.muted = !video.muted;
    sound.textContent = video.muted ? "🔇" : "🔊";
    sound.title = video.muted ? "Activer le son" : "Couper le son";
    // The click is the user gesture the browser wants before it will play with
    // sound, so this is the one place `play()` is worth asking for by hand.
    if (!video.muted) video.play().catch(() => {});
  };

  // « Incrustation permutable » : le même geste dans les deux sens, la vidéo
  // dans le coin de la scène ou la scène dans le coin de la vidéo. Un bouton
  // plutôt qu'un quatrième choix dans la liste — c'est la même mise en page, vue
  // de l'autre côté, et on la permute en la regardant.
  swap.textContent = "⇄";
  swap.title = "Permuter l'incrustation";

  bar.append(label, drift, state, swap, sound);
  wrap.append(bar, video);
  stage.appendChild(wrap);

  const clock = new VideoSyncClock(video);

  // One permanent chain, never a callback armed per seek (#4): an abandoned one
  // arrives late and it is the *next* request that collects it, reading the
  // frame before. `mediaTime` is the PTS of the frame actually presented, which
  // is what makes both the domain offset and the true drift measurable at all.
  if (video.requestVideoFrameCallback) {
    const presented = (_now, meta) => {
      clock.onPresentedFrame(meta.mediaTime);
      video.requestVideoFrameCallback(presented);
    };
    video.requestVideoFrameCallback(presented);
  }

  let key      = null;    // "session/take" currently loaded
  let hasTake  = false;   // a take is on screen at all — false live, false at boot
  let hasVideo = false;
  let measuring = false;
  let layout   = DEFAULT_LAYOUT;
  let swapped  = false;
  let onSwap   = null;    // the page persists the choice; this module holds none

  async function measure() {
    if (measuring || !video.duration) return;
    measuring = true;
    try { await clock.measureOffset(); } finally { measuring = false; }
  }

  video.addEventListener("loadeddata", () => { setTimeout(measure, REMEASURE_MS); });
  // Dropping the source to show a take that has no video fires `error` too, and
  // that one is not a failure — it is the absence, which the bar has already
  // named. Only a file that was expected and will not decode is worth saying.
  video.addEventListener("error", () => {
    if (hasVideo) label.textContent = "vidéo illisible";
  });
  // A hidden document presents no frame, so a measurement started there times
  // out. Ask again when the page comes back — the cost is one seek, and the
  // alternative is a file whose edit-list constant is never noticed.
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && hasVideo && !clock.offsetMeasured) measure();
  });

  /**
   * Which take is on screen. Called with what the session tree already holds —
   * the stored `video_file` and both anchors — so nothing here builds a path or
   * re-reads metadata.
   *
   * Idempotent: it is called at 4 Hz from the snapshot and on every change of
   * the take selector.
   */
  function setTake(info) {
    const k = info ? `${info.session}/${info.take}` : null;
    if (k === key) return;
    key = k;

    hasTake  = !!info;
    hasVideo = !!(info && info.video_file);
    applyView();
    clock.newFile();
    clock.setAlignment(info ? info.onset_imu_s : null,
                       info ? info.onset_video_s : null);

    if (!hasVideo) {
      video.removeAttribute("src");
      video.load();
      label.textContent = "aucune vidéo";
      return;
    }
    label.textContent = `${info.take} — ${info.video_file}`;
    video.src = `/api/sessions/${enc(info.session)}/takes/${enc(info.take)}/video`;
    video.load();
  }

  // The one place the description is applied to the DOM. Everything it writes
  // comes from `describe()`; the only thing decided here is *when* to ask —
  // a change of take, a change of layout, and the bar's own slow cadence.
  let sceneKey = null;

  function applyView() {
    const s = clock.stats;
    // A hand on the cursor is a driver like the replay is: without this, an
    // idle take being swept would report "inactif" over a picture that is
    // following the cursor exactly.
    const driven = clock.active || clock.scrubbing;
    const d = describe({ layout, swapped, hasTake, hasVideo,
                         state: s.state, driven });

    // Two attributes, two readers: `data-mode` places the frame, `data-scene`
    // places the canvas — the stage is the only element that can be told the
    // second, the canvas not being this module's to style.
    if (wrap.dataset.mode   !== d.video) wrap.dataset.mode   = d.video;
    if (stage.dataset.scene !== d.scene) stage.dataset.scene = d.scene;
    wrap.classList.toggle("hatched", d.hatched);
    wrap.classList.toggle("idle", d.greyed);
    if (state.textContent !== d.badge) state.textContent = d.badge;
    // Permuting is a gesture on an inset, and on a take that has one.
    swap.hidden  = d.layout !== "incrustation" || !hasVideo;
    sound.hidden = !hasVideo;

    // Three.js state is not free to rewrite four times a second, and neither is
    // a `follow` checkbox the user may be looking at. Keyed on exactly what the
    // callback reads: adding `scene` here would make a ⇄ rewrite the renderer
    // for a change it does not care about, which is what the gate is for.
    const k = `${d.follow}|${d.transparent}`;
    if (onView && k !== sceneKey) { sceneKey = k; onView(d); }
    return d;
  }

  // The bar, refreshed on its own slow cadence — the frame path runs at ~100 Hz
  // and has no business touching the DOM.
  //
  // "suit le replay" is the nominal case and is not shown: a permanent badge
  // stops being read within a minute. What is shown is every case where the
  // picture is *not* the take's — not aligned, out of range, detached — because
  // a frozen picture looks exactly like a correct one on a still instant.
  setInterval(() => {
    const s = clock.stats;
    applyView();
    // Hatched, the bar keeps its name and nothing else: a drift, a rate or a
    // domain constant left there would be the *previous* take's, printed over
    // the frame of a take that has no picture at all.
    if (!hasVideo) { drift.textContent = ""; return; }

    const bits = [];
    if (clock.active && Number.isFinite(s.driftMedia)) {
      bits.push(`${s.driftMedia >= 0 ? "+" : ""}${(s.driftMedia * 1000).toFixed(0)} ms`);
    }
    // A rate the browser refused in silence would make the video diverge with
    // nothing to say so — measured accepted as asked from ×0.25 to ×4, which is
    // exactly why a refusal has to be visible rather than assumed impossible.
    // Detached, nothing is written and both figures are the last pass's: a chip
    // built from them would be reporting a refusal that is no longer happening.
    if (clock.rateRefused && !s.detached) {
      bits.push(`taux ×${s.rateAsked.toFixed(2)} refusé (×${s.rateGot.toFixed(2)})`);
    }
    // Named for what it is — the constant between the two *domains* — and shown
    // only past a frame. "Décalage" alone would read as the offset between the
    // two anchors, which is precisely the value this whole design refuses to
    // have (ADR 0001), and a sub-frame residue on every take is noise.
    if (clock.offsetMeasured && Math.abs(clock.offset) > 0.04) {
      bits.push(`domaines ${(clock.offset * 1000).toFixed(0)} ms`);
    }
    drift.textContent = bits.join(" · ");
  }, 250);

  swap.onclick = () => {
    swapped = !swapped;
    applyView();
    if (onSwap) onSwap(swapped);
  };

  const api = {
    clock,
    setTake,

    /**
     * The layout, decided by the page (which owns the control and the storage)
     * and applied here. `onSwapped` is how the ⇄ button gets persisted without
     * this module knowing what `localStorage` is.
     */
    setLayout(name, isSwapped, onSwapped) {
      layout  = name;
      swapped = !!isSwapped;
      onSwap  = onSwapped || null;
      return applyView();
    },
    onFrame(d) { clock.onFrame(d.t); },
    onMeta(m)  { if (m.topic === "reset") clock.onReset(); },
    onPlayback(p) { clock.onPlayback(p); },

    // The cursor. `scrub` says whether there is a picture at that instant at
    // all, which is what lets the page grey the inset out rather than leave a
    // frozen frame looking like a correct one.
    scrub(tS) { return hasVideo && clock.scrub(tS); },
    endScrub() {
      clock.endScrub();
      // A sweep begun before the file had been measured gave that measurement
      // up (it was a short muted run, and the cursor stopped the element). Ask
      // again now the hand is off, rather than leave the domain constant at a
      // zero nobody measured.
      if (hasVideo && !clock.offsetMeasured) measure();
    },
  };

  // The one handle out of this module, and nothing on the page reads it: it is
  // what `_harness.js`'s `record()` attaches to, so that re-running the drift
  // campaign after touching a constant is a line in the console rather than a
  // build. Without it the only measurable drift is what the bar prints at 4 Hz,
  // which is a twenty-fifth of the samples and no resync count at all.
  window.__vizVideo = api;
  return api;
}
