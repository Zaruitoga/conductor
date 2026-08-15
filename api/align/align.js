// align.js — the alignment page: see the take, designate the frame, confirm.
//
// A take and its video are two files sitting side by side with nothing tying
// them together. What ties them is an *alignment*: two anchors locating the same
// start of movement — wheel still on the ground, then lifted sharply — on the
// take's timeline and in the video. The server proposes the inertial one from
// the raw gyro (`GET …/onset`, ADR 0001) and never stores it; this page is where
// that proposition is judged, the video frame designated to the frame, and the
// pair written once.
//
// A page of its own, mounted like `/viz/` is: the panel is a show surface and
// the visualiser's main-thread budget is capped on purpose, while this runs a
// video decoder next to a canvas. No build, no bundler, no CDN — raw ES modules,
// so it works offline like every other surface here.
//
// Three things it will not do, each decided rather than forgotten:
//   • **no 3D wheel** — the question is "did the detection point at the right
//     start", which is settled on the curve and the picture; when it is wrong it
//     is wrong by seconds, and one changes candidate;
//   • **no take-time timeline** — built and removed in the prototype, it said
//     nothing the video scrubber does not;
//   • **no badge of detectability** — a take's state is read from stored data
//     only (ADR 0001: the listing is served at 4 Hz and recomputes nothing).

import { CurveView } from "./curve.js";
import { VideoClock } from "./video.js";

const $ = (id) => document.getElementById(id);
const enc = encodeURIComponent;
const takeUrl = (s, t, suffix = "") =>
  `/api/sessions/${enc(s)}/takes/${enc(t)}${suffix}`;

// Same contract as the panel's js/api.js — reimplemented here rather than
// imported, exactly as api/viz/viz.js does: a second surface does not take a
// dependency on the panel's module graph for twelve lines.
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

const S = {
  sessions: [],        // the full tree, as GET /api/sessions gives it
  session: null,       // session name
  take: null,          // the take dict (TakeMeta + videos_found)
  onset: null,         // {candidats, motif, courbe, duree_s}
  onsetError: null,
  pick: 0,             // index into picks() — the inertial choice held
  clock: null,         // VideoClock over the <video>
  loading: null,       // AbortController for the current load's listeners
  stageMsg: null,      // the degraded-state panel currently over the video, if any
  videoError: false,
  token: 0,            // load generation — a late event from a previous take is dropped
  dragging: false,
  detail: false,       // frame-by-frame mode — entered by the arrows themselves
  pinned: null,        // {t, canvas} — the pinned frame of rest
  blink: false,        // the pinned frame is showing instead of the current one
  saving: false,
};

const takes = () => S.sessions.find((s) => s.name === S.session)?.takes ?? [];
const cands = () => S.onset?.candidats ?? [];
const curveSamples = () => S.onset?.courbe?.gyro_norm ?? [];
const aligned = (t) => t && t.onset_imu_s != null && t.onset_video_s != null;

// Video time → take time. The two anchors define the translation and nothing
// else — never their difference, which is a residue either side recomputes
// (ADR 0001). Undefined until an alignment exists, which is exactly why the
// cursor below only runs on an aligned take.
const toTakeTime = (videoT) =>
  aligned(S.take) && videoT != null
    ? videoT - S.take.onset_video_s + S.take.onset_imu_s
    : null;

// The inertial choices, in time order.
//
// The propositions, plus the stored anchor when it is none of them. That last
// case is not a curiosity: retuning the detection is *allowed* to move the
// candidates (ADR 0001 — it only ever proposes), so a take confirmed before a
// constant changed keeps an anchor that matches nothing. Making it a choice of
// its own is what lets the video frame be re-posed without silently dragging the
// inertial anchor onto candidate 1 — moving the error rather than fixing it.
//
// Which choice the anchor was taken from is *derived*, never stored: a take
// records the anchor and never the proposition it came from (ADR 0001).
const ANCHOR_EPS = 1e-6;   // the anchor is written *from* a choice, so exact bar float noise
function picks() {
  const list = cands().map((c, i) => ({ t: c.t_s, silence: c.silence_s, cand: i }));
  const a = S.take?.onset_imu_s;
  if (a != null && !list.some((p) => Math.abs(p.t - a) < ANCHOR_EPS)) {
    list.push({ t: a, silence: null, cand: -1 });
    list.sort((x, y) => x.t - y.t);
  }
  return list;
}

const imuAnchor = () => picks()[S.pick]?.t ?? null;

/** Index in picks() of the stored anchor, or -1 when the take is not aligned. */
function anchorPick() {
  const a = S.take?.onset_imu_s;
  if (a == null) return -1;
  return picks().findIndex((p) => Math.abs(p.t - a) < ANCHOR_EPS);
}

const curve = new CurveView($("curve"), {
  onPick: (t) => {          // clicking the curve picks the nearest choice…
    const list = picks();
    if (!list.length) return;
    S.pick = list.reduce(
      (best, p, i) => Math.abs(p.t - t) < Math.abs(list[best].t - t) ? i : best, 0);
    renderChrome();
  },
  onView: () => renderNote(),
});

// ── Loading ─────────────────────────────────────────────────────────────────

async function loadTree() {
  const data = await api("GET", "/api/sessions");
  S.sessions = data.sessions ?? [];
}

async function selectSession(name) {
  S.session = name;
  fillSelects();
  await selectTake(takes()[0]?.name ?? null);
}

// The URL follows the take instead of merely naming it at boot. The query is an
// entry point — the panel's take list and the viz's selector link here — and an
// address that stops being true after the first choice cannot be copied or
// reloaded onto what is on screen.
//
// `replaceState`, never `pushState`: Back must return where the page was opened
// from, which is what the header's ← does too. Walking eight takes and needing
// eight presses to leave is the wrong gesture for a post-production tool.
//
// It runs on the resolved take, not on the requested one, so a query naming a
// take that no longer exists is normalised to the one actually displayed.
function syncUrl() {
  if (!S.session) return;
  const q = new URLSearchParams({ session: S.session });
  if (S.take) q.set("take", S.take.name);
  history.replaceState(null, "", `${location.pathname}?${q}`);
}

async function selectTake(name) {
  const token = ++S.token;
  S.clock?.dispose();
  S.clock = null;
  S.take = takes().find((t) => t.name === name) ?? null;
  S.onset = null;
  S.onsetError = null;
  S.videoError = false;
  S.pick = 0;
  S.detail = false;
  S.pinned = null;
  showBlink(false);

  const video = $("v");
  video.pause();
  video.removeAttribute("src");
  video.load();
  curve.setTake([], 1);
  fillSelects();
  syncUrl();
  renderChrome();
  renderNow();
  if (!S.take) return;

  if (S.take.video_file) openVideo(S.take, token);

  // The curve is a full CSV pass on the server — about a second for a long take
  // — so it lands after the video rather than gating it.
  try {
    S.onset = await api("GET", takeUrl(S.session, S.take.name, "/onset"));
  } catch (e) {
    S.onsetError = e.message;
  }
  if (token !== S.token) return;
  curve.setTake(curveSamples(), S.onset?.duree_s ?? 1);
  // On an aligned take, open on the choice the anchor was taken from rather than
  // on the first one — otherwise the page would highlight a proposition that was
  // passed over.
  S.pick = Math.max(anchorPick(), 0);
  renderChrome();
  renderNow();
}

function openVideo(take, token) {
  const video = $("v");
  // One <video> serves every take, so each load owns an AbortController: a take
  // whose file never loads and never errors would otherwise leave its listeners
  // on the shared element for the rest of the session.
  S.loading?.abort();
  S.loading = new AbortController();
  const { signal } = S.loading;

  video.src = takeUrl(S.session, take.name, "/video");
  S.clock = new VideoClock(video).on(() => { if (token === S.token) renderNow(); });

  video.addEventListener("loadeddata", async () => {
    if (token !== S.token) return;
    await S.clock.decide();       // does this browser report presented frames?
    if (token !== S.token) return;
    renderChrome();
    renderNow();
    // The file's cadence is measured, never read from a container (`ffprobe` is
    // not installed, and a binary dependency for a step size was ruled out) —
    // and it only ever sizes the probe. It costs a handful of seeks, so it runs
    // after the page is already usable rather than in front of it.
    await S.clock.measure();
    if (token === S.token) { renderChrome(); renderNow(); }
  }, { once: true, signal });

  video.addEventListener("error", () => {
    if (token !== S.token) return;
    S.videoError = true;
    S.clock?.dispose();
    S.clock = null;
    renderChrome();
    renderNow();
  }, { once: true, signal });
}

// The scan proposes, `video_file` records: adopting is an explicit PATCH, never
// something a GET does behind the operator's back — they are the one who can
// tell whether the file the folder holds is the one they mean, and which of two
// it is (see SessionManager.scan_videos).
async function adopt(filename) {
  try {
    await api("PATCH", takeUrl(S.session, S.take.name), { video_file: filename });
  } catch (e) {
    return flash(`Adoption refusée : ${e.message}`);
  }
  const name = S.take.name;
  await loadTree();
  await selectTake(name);
}

// ── Confirming ──────────────────────────────────────────────────────────────

// The two anchors, written together and once. `TakeUpdate` refuses a body
// carrying one without the other — an alignment is indivisible — which is what
// keeps "not yet aligned" a state needing no field of its own.
//
// The video anchor is the PTS of the frame the browser says it is *displaying*,
// never the instant we asked for: `currentTime` read back is the request, not
// the frame. Re-posing an alignment is the same call, which is why the button
// only changes its words.
async function confirmAlign() {
  const imu = imuAnchor();
  const video = S.clock?.media;
  if (imu == null || video == null || S.saving) return;
  S.saving = true;
  renderAnchors();
  try {
    await api("PATCH", takeUrl(S.session, S.take.name),
              { onset_imu_s: imu, onset_video_s: video });
  } catch (e) {
    S.saving = false;
    return flash(`Alignement refusé : ${e.message}`);
  }
  S.saving = false;
  // Re-read the take rather than patching the local copy: what the page shows as
  // posed must be what is on disk. Only the tree is reloaded — going through
  // `selectTake` would reload the video and re-read the whole CSV, losing the
  // frame just designated.
  const name = S.take.name;
  await loadTree();
  S.take = takes().find((t) => t.name === name) ?? S.take;
  S.pick = Math.max(anchorPick(), 0);
  fillSelects();          // the take's own option carries its state — it just changed
  renderChrome();
  renderNow();
}

// ── Chrome (rebuilt on state changes only) ──────────────────────────────────

function fillSelects() {
  const sessions = S.sessions;
  $("session").innerHTML = sessions.map((s) =>
    `<option value="${attr(s.name)}"${s.name === S.session ? " selected" : ""}>
       ${esc(s.title || s.name)}</option>`).join("");
  $("take").innerHTML = takes().map((t) =>
    `<option value="${attr(t.name)}"${t.name === S.take?.name ? " selected" : ""}>
       ${esc(t.name)} — ${state(t).label}</option>`).join("");
  $("take").disabled = !takes().length;
}

// The state of a take, from stored data and nothing else. Never a detection
// badge: ADR 0001 rules one out, and the listing this reads is served at 4 Hz by
// `active_tree()`, which recomputes nothing.
function state(t) {
  if (!t.video_file) return { label: "sans vidéo", cls: "none" };
  if (!aligned(t)) return { label: "non aligné", cls: "todo" };
  return { label: "aligné", cls: "ok" };
}

function renderChrome() {
  const t = S.take;
  const st = t ? state(t) : { label: "—", cls: "none" };
  $("badge").textContent = st.label;
  $("badge").className = `pill ${st.cls}`;

  renderVideoLine();
  renderStage();
  renderCandidates();
  renderAnchors();
  renderNote();
  curve.draw(curveState());
}

// What file the page found, and what it is playing. Named, never typed in:
// `video_file` is a bare filename so a take stays movable and archivable.
function renderVideoLine() {
  const el = $("video-line");
  const t = S.take;
  if (!t) { el.textContent = ""; return; }
  if (t.video_file) {
    // An empty scan is not a reason to stay quiet: with a `video_file` set it is
    // exactly how "the file named is no longer there" looks.
    const gone = !(t.videos_found ?? []).includes(t.video_file);
    el.innerHTML = `<span class="mono">${esc(t.video_file)}</span>`
      + (gone ? ` <span class="bad">absent du dossier</span>` : "");
    return;
  }
  const found = t.videos_found ?? [];
  if (!found.length) { el.innerHTML = `<span class="dim">aucune vidéo</span>`; return; }
  el.innerHTML = `<span class="dim">trouvé dans le dossier :</span>`
    + found.map((f) =>
        `<button class="ghost" data-adopt="${attr(f)}">${esc(f)}</button>`).join("");
  el.querySelectorAll("[data-adopt]").forEach((b) => {
    b.onclick = () => adopt(b.dataset.adopt);
  });
}

// Two of the four degraded states live here — "no video in the folder" and "the
// file will not decode" — and they are told apart on screen because they call
// for two different gestures: copy a file in, versus this one is unusable.
function renderStage() {
  const box = $("stage-msg");
  const t = S.take;
  let html = null;

  if (!t) {
    html = `<div class="msg"><b>Aucun take.</b>
      <span>Cette séance n'a pas d'enregistrement lisible.</span></div>`;
  } else if (!t.video_file && !(t.videos_found ?? []).length) {
    html = `<div class="msg"><b>Aucune vidéo dans le dossier de ce take.</b>
      <span>Copier le fichier dans
        <code>sessions/${esc(S.session)}/takes/${esc(t.name)}/</code>
        puis recharger. Les candidats sont proposés quand même : ils n'attendent
        pas les rushes.</span></div>`;
  } else if (!t.video_file) {
    html = `<div class="msg warn"><b>Une vidéo est là, ce take n'en a pas encore.</b>
      <span>Choisir le fichier dans l'en-tête pour l'attribuer à ce take.</span></div>`;
  } else if (S.videoError) {
    const gone = !(t.videos_found ?? []).includes(t.video_file);
    html = `<div class="msg bad"><b>Le fichier ne se décode pas.</b>
      <span><span class="mono">${esc(t.video_file)}</span> — ${gone
        ? "il n'est plus dans le dossier du take."
        : "le navigateur refuse ce flux (codec ou fichier tronqué)."}
        Rien à aligner tant qu'il n'est pas lisible.</span></div>`;
  }

  S.stageMsg = html;
  box.innerHTML = html ?? "";
  box.hidden = !html;
  $("stage").classList.toggle("empty", !!html);
  setProp($("hud"), "hidden", hudHidden());
}

// One predicate, two callers. Stated twice it was stated differently — the
// per-frame path re-derived it by reading `stage-msg`'s `hidden` back out of the
// DOM, which is the same rule spelled backwards and one edit away from
// disagreeing with itself.
const hudHidden = () => !S.clock || !!S.stageMsg;

// The other two degraded states: "no gyro stream in this take" is not "nothing
// detected". Take 001 of the reference session is 171 rows of GAME_RV — the
// method does not apply to it — and that is a different sentence from a rule
// that ran and found nothing. Told apart the way `propose()` itself tells them
// apart (an empty series *is* the absence of a stream), not by matching prose.
function renderCandidates() {
  const box = $("cands");
  if (!S.take) { box.innerHTML = ""; return; }
  if (S.onsetError) {
    box.innerHTML = `<span class="bad">Proposition indisponible : ${esc(S.onsetError)}</span>`;
    return;
  }
  if (!S.onset) { box.innerHTML = `<span class="dim">lecture du take…</span>`; return; }
  if (!curveSamples().length) {
    box.innerHTML = `<span class="warn">Aucun flux gyro dans ce take —
      la méthode ne s'applique pas.</span>
      <span class="dim">(${esc(S.onset.motif ?? "")})</span>`;
    return;
  }
  if (!cands().length && !picks().length) {
    box.innerHTML = `<span class="warn">Rien détecté.</span>
      <span class="dim">${esc(S.onset.motif ?? "")}</span>`;
    return;
  }
  // Each choice carries its own evidence — the rest that precedes it — so it can
  // be judged at a glance instead of being taken on trust as a bare instant. The
  // stored anchor, when it is not one of the propositions, is listed among them
  // and says so.
  const anc = anchorPick();
  box.innerHTML = picks().map((p, i) =>
    `<button class="cand${i === S.pick ? " on" : ""}${i === anc ? " kept" : ""}" data-i="${i}">
       <b>${p.t.toFixed(2)} s</b>
       <span class="dim">${p.silence == null
          ? "ancre posée · hors candidats"
          : `repos ${fmtSec(p.silence)}${i === anc ? " · ancre" : ""}`}</span>
     </button>`).join("");
  box.querySelectorAll("[data-i]").forEach((b) => {
    b.onclick = () => { S.pick = +b.dataset.i; reveal(); renderChrome(); };
  });
}

// The confirmation is a **state**, not a past event. Once posed, the two anchors
// show what is stored and stop moving — they used to show the live position, so
// they scrolled while one verified, which is to say they showed something other
// than what is on disk. The button then only changes its words: re-posing is the
// same write.
function renderAnchors() {
  const posed = aligned(S.take);
  const imu = imuAnchor();
  const media = S.clock?.media ?? null;

  setProp($("lock"), "hidden", !posed);
  setText($("a-imu"), fmtS(posed ? S.take.onset_imu_s : imu));
  setText($("a-vid"), fmtS(posed ? S.take.onset_video_s : media));
  setText($("a-delta"), posed
    ? fmtS(S.take.onset_video_s - S.take.onset_imu_s)
    : (imu != null && media != null ? fmtS(media - imu) : "—"));

  const n = picks().length;
  setText($("a-note"), posed
    ? (imu != null && Math.abs(imu - S.take.onset_imu_s) > ANCHOR_EPS
        ? `· choix courant ${fmtS(imu)} — ⏎ repose les deux ancres`
        : "")
    : (n > 1 ? `· choix ${S.pick + 1}/${n}` : ""));

  setText($("pin-state"), S.pinned
    ? `épinglée ${fmtS(S.pinned.t)} — maintenir B`
    : (S.clock ? "E épingle une frame de repos" : ""));

  const ok = $("ok");
  setText(ok, S.saving ? "…" : posed ? "Reposer sur la frame courante"
                                     : "Confirmer l'alignement");
  ok.className = posed ? "ghost" : "";
  setProp(ok, "disabled", S.saving || imu == null || media == null);
}

function renderNote() {
  const n = picks().length;
  const span = curve.t1 - curve.t0;
  const bits = [];
  if (n) bits.push(`choix ${S.pick + 1}/${n}`);
  if (S.onset) {
    bits.push(curve.zoomed
      ? `fenêtre ${span.toFixed(span < 1 ? 3 : 2)} s sur ${curve.dur.toFixed(2)} s`
      : `vue complète, ${curve.dur.toFixed(2)} s`);
    bits.push(`${curveSamples().length} échantillons`);
  }
  if (aligned(S.take)) {
    const i = anchorPick();
    const p = picks()[i];
    bits.push(p && p.cand >= 0
      ? `ancre posée sur le candidat ${p.cand + 1}`
      : `ancre posée à ${S.take.onset_imu_s.toFixed(2)} s, hors candidats`);
    bits.push("le curseur rouge suit la vidéo");
  } else {
    bits.push("curseur de lecture une fois l'alignement posé");
  }
  setText($("note"), bits.join(" · "));
}

function curveState() {
  const p = picks()[S.pick];
  return {
    candidates: cands(),
    selected: p ? p.cand : -1,        // index into the propositions, or -1
    anchor: S.take?.onset_imu_s ?? null,   // what is stored, beside what is proposed
    anchorOn: !!p && p.cand < 0,      // the stored anchor is itself the choice held
    playhead: toTakeTime(S.clock?.media ?? null),
  };
}

// ── Now (the parts that move with the video) ────────────────────────────────
//
// Split from the chrome on purpose: this runs on every presented frame, and
// rebuilding the candidate list at 60 Hz would make a chip impossible to click.

function renderNow() {
  const clock = S.clock;
  const video = $("v");
  const dur = Number.isFinite(video.duration) ? video.duration : 0;

  setProp($("play"), "disabled", !clock || !dur);
  setText($("play"), video.paused ? "▶" : "⏸");
  setProp($("scrub"), "disabled", !clock || !dur);
  setProp($("scrub"), "max", String(dur || 1));
  if (clock && !S.dragging) $("scrub").value = clock.media;

  // The video anchor, marked on the réglette it was posed with.
  const mark = $("scrub-mark");
  const posed = aligned(S.take) && dur > 0;
  setProp(mark, "hidden", !posed);
  if (posed) mark.style.left = `${(S.take.onset_video_s / dur) * 100}%`;

  setProp($("hud"), "hidden", hudHidden());
  if (clock) {
    setText($("hud-time"), `${clock.media.toFixed(3)} s`);
    setText($("hud-mode"), S.detail ? "détail — frame par frame" : "navigation");
    setProp($("hud-mode"), "className", S.detail ? "chip on" : "chip");
    setText($("hud-cadence"), `cadence ${clock.measured ? "mesurée" : "supposée"} `
      + `${clock.fps.toFixed(2)} fps`);
    setText($("hud-src"), clock.rvfc === false
      ? "repli currentTime" : clock.rvfc ? "frame présentée" : "…");
  }
  setText($("dur"), dur ? `/ ${dur.toFixed(2)} s` : "—");

  renderAnchors();
  // Only worth a redraw when something on the curve actually moves.
  if (aligned(S.take) && clock) curve.draw(curveState());
}

// ── Gestures ────────────────────────────────────────────────────────────────

function cycle(d) {
  const n = picks().length;
  if (!n) return;
  S.pick = (S.pick + d + n) % n;
  reveal();
  renderChrome();
}

// Changing choice while zoomed in must move the window, or the choice happens
// off screen.
function reveal() { curve.reveal(picks()[S.pick]?.t); }

function togglePlay() {
  const video = $("v");
  if (!S.clock || !Number.isFinite(video.duration)) return;
  video.paused ? video.play().catch(() => {}) : video.pause();
}

// The arrows enter detail mode by themselves: no mode key to learn. Stepping a
// playing video would fight the playback — and the frame-exact guarantee only
// holds in pause — so entering pauses.
function enterDetail() {
  if (!S.clock) return false;
  if (!S.detail) {
    S.detail = true;
    $("v").pause();
    renderNow();
  }
  return true;
}

function leaveDetail() {
  if (!S.detail) return false;
  S.detail = false;
  renderNow();
  return true;
}

// The comparator is a toggle, never a strip of frames: five side by side are
// 1 px of movement. A frame of *rest* is pinned and flashed on demand, so the
// gap accumulates from it — a soft start eventually leaps out, where an N ↔ N−1
// comparison compares against a moving target.
function pin() {
  const video = $("v");
  if (!S.clock || !video.videoWidth) return;
  const c = document.createElement("canvas");
  c.width = video.videoWidth;
  c.height = video.videoHeight;
  c.getContext("2d").drawImage(video, 0, 0);
  S.pinned = { t: S.clock.media, canvas: c };
  renderChrome();
}

function showBlink(on) {
  const want = on && !!S.pinned;
  if (S.blink === want) return;
  S.blink = want;
  const el = $("pinned");
  if (want) {
    el.width = S.pinned.canvas.width;
    el.height = S.pinned.canvas.height;
    el.getContext("2d").drawImage(S.pinned.canvas, 0, 0);
  }
  el.hidden = !want;
  setProp($("blink-tag"), "hidden", !want);
}

function flash(msg) {
  $("note").innerHTML = `<span class="bad">${esc(msg)}</span>`;
  setTimeout(renderNote, 5000);
}

// The panel's shortcut doctrine (api/static/js/shortcuts.js), reimplemented here
// rather than imported — the same choice api/viz/viz.js made for its `api()`
// helper. Suppressed while typing and under any modifier, so they never fight
// with text entry or a browser shortcut. Shift is not a modifier here: it is
// half the key map.
const TYPING = new Set(["INPUT", "TEXTAREA", "SELECT"]);
const isTyping = (el) => !!el && (TYPING.has(el.tagName) || el.isContentEditable);

// Horizontal = video time, vertical = inertial choice.
function initKeys() {
  document.addEventListener("keydown", (e) => {
    if (isTyping(e.target)) return;
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    switch (e.key) {
      case "ArrowRight":
      case "ArrowLeft": {
        e.preventDefault();
        if (!enterDetail()) return;
        const d = e.key === "ArrowRight" ? 1 : -1;
        e.shiftKey ? S.clock.jump(d * 10) : S.clock.step(d);
        break;
      }
      // Up goes to the *next* choice, i.e. later in the take. They are ordered
      // in time and drawn on a timeline, so the axis they answer to is the
      // curve's, not a list's — where up would mean the item above. Both axes
      // now run the same way: forward.
      case "ArrowUp":   e.preventDefault(); cycle(1); break;
      case "ArrowDown": e.preventDefault(); cycle(-1); break;
      case "e": case "E":
        if (e.repeat) return;
        e.preventDefault(); pin(); break;
      case "b": case "B":
        e.preventDefault(); showBlink(true); break;
      case "Enter":
        e.preventDefault(); confirmAlign(); break;
      case "Escape":
        if (leaveDetail()) e.preventDefault();
        break;
      case " ":
        // Space also activates a focused button — let that win.
        if (e.target instanceof Element && e.target.closest("button, a")) return;
        e.preventDefault(); leaveDetail(); togglePlay(); break;
      default: break;
    }
  });
  // The flash is a *held* key: it must die with the keyup, and also when the
  // window loses focus mid-press — where no keyup is ever delivered.
  document.addEventListener("keyup", (e) => {
    if (e.key === "b" || e.key === "B") showBlink(false);
  });
  addEventListener("blur", () => showBlink(false));
}

// ── Little helpers ──────────────────────────────────────────────────────────

const esc = (s) => String(s ?? "").replace(/[&<>]/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
const attr = (s) => esc(s).replace(/"/g, "&quot;");
const fmtSec = (s) => (s >= 10 ? s.toFixed(1) : s.toFixed(2)) + " s";
const fmtS = (s) => (s == null ? "—" : `${s.toFixed(3)} s`);

// Write only on change — `renderNow` runs on every presented frame, and the
// panel's `dom.js` doctrine (reimplemented here, like the `api()` helper) is
// what keeps a 60 Hz refresh from clobbering a text selection or a control the
// user is in the middle of using.
function setText(el, v) { if (el.textContent !== v) el.textContent = v; }
function setProp(el, k, v) { if (el[k] !== v) el[k] = v; }

// ── Boot ────────────────────────────────────────────────────────────────────

(async function boot() {
  initKeys();
  const video = $("v");

  ["play", "pause", "ended"].forEach((ev) =>
    video.addEventListener(ev, renderNow));
  video.addEventListener("loadedmetadata", renderNow);
  $("play").onclick = () => { leaveDetail(); togglePlay(); };
  $("ok").onclick = confirmAlign;

  // The range keeps focus after a click, so its value would freeze at the last
  // point clicked while playback moved on — and the arrow keys would go to it
  // instead of the choices. Follow the pointer, not the focus.
  const scrub = $("scrub");
  scrub.addEventListener("pointerdown", () => { S.dragging = true; });
  addEventListener("pointerup", () => {
    if (!S.dragging) return;
    S.dragging = false;
    scrub.blur();
    renderNow();
  });
  scrub.oninput = (e) => { leaveDetail(); S.clock?.seek(+e.target.value); };

  // Blurred after the choice, for the same reason the scrubber is: a <select>
  // that keeps focus swallows `↑`/`↓`, and here they would change *take* — a
  // whole CSV read per keypress — instead of cycling choices, which is the
  // gesture this page is built around. Blurring keeps the panel's doctrine
  // intact (shortcuts stay suppressed while a control is in use) rather than
  // carving SELECT out of it.
  $("session").onchange = (e) => { e.target.blur(); selectSession(e.target.value); };
  $("take").onchange = (e) => { e.target.blur(); selectTake(e.target.value); };
  addEventListener("resize", () => curve.draw());

  // The URL names a take when the page is opened from elsewhere, and `syncUrl`
  // keeps it true from there on.
  const q = new URLSearchParams(location.search);
  try {
    await loadTree();
  } catch (e) {
    $("cands").innerHTML = `<span class="bad">GET /api/sessions : ${esc(e.message)}</span>`;
    return;
  }
  const wanted = S.sessions.find((s) => s.name === q.get("session"));
  S.session = (wanted ?? S.sessions[0])?.name ?? null;
  fillSelects();
  const take = takes().find((t) => t.name === q.get("take")) ?? takes()[0];
  await selectTake(take?.name ?? null);
})();
