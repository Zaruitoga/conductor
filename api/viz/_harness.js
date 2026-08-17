// _harness.js — the sync clock's bench. **Development only**, deliberately
// outside `tests/run.py`: that suite is Python and dependency-free, and this
// needs a browser to run at all (same standing as `/align/_harness.js`).
//
//     (await import('/viz/_harness.js')).run()      // from the page's console
//
// It exists because the two costliest defects of this mechanism are *silent*
// (spec #19, "Testing decisions"): adding the two domains leaves the video
// following the movement perfectly, offset by a fixed number of frames, and a
// bias that never crosses its threshold looks exactly like a video that is
// simply a little late. Neither shows up as a jolt, and neither is visible on a
// still instant — which is most of a take.
//
// Everything here is virtual, including real time: the sync clock counts its
// seek debounce in **wall** seconds, so `performance.now` is patched and driven
// by the bench. That is what lets a case feed five seconds of model time into a
// third of a second of wall and check that the debounce still holds — exactly
// the discrimination between a debounce counted in replay time and one counted
// in wall time, which cost nine jolts a real second at ×4 the first time.
//
// A case bites or it is worthless. To check that one does, point `run()` at a
// copy of the clock with the correction removed:
//
//     (await import('/viz/_harness.js')).run('./_sync_clock_sans_correctif.js')
//
// `run(clockMod, sweepMod, layoutMod)` takes the three modules it exercises, so
// a copy of any one of them with a correction removed can be pointed at. The
// layout cases are the only synchronous ones and touch no DOM: `layout.js`'s
// `describe()` is the single decision behind both appliers, which is what makes
// "a take with no video folds the pane *here* and hatches the frame *there*, and
// never carries a badge" checkable rather than merely intended.

/**
 * The drift campaign, on the real page: what the ticket's table is made of.
 *
 *     const rec = (await import('/viz/_harness.js')).record();
 *     // …start a replay, let it run a whole pass…
 *     rec.stop()
 *
 * It samples on the model's own frames (~100 Hz) by wrapping the clock's hot
 * path, which is the only cadence at which a ±20 ms signal exists at all — the
 * bar prints one sample in twenty-five, and no resync count.
 *
 * Two rules it shares with the bench, both learnt the expensive way:
 *
 *   • the drift reported is `mediaTime` − target, both in the reading domain.
 *     `currentTime` is the command signal, not what is on screen.
 *   • a hard resync's own transient is not drift. At a loop turnover the picture
 *     is still fifty seconds away while the timeline has restarted, and one such
 *     sample flattens the whole ±100 ms signal the measurement is about. The
 *     resync is counted; its value is not recorded.
 *
 * **Run it with the window in the foreground.** Chrome pauses a muted video in a
 * hidden tab, so a campaign launched in a background tab measures Chrome's power
 * saving and nothing else.
 */
export function record({ quietMs = 300 } = {}) {
  const api = window.__vizVideo;
  if (!api) throw new Error("aucune vidéo montée sur cette page");
  const clock = api.clock;
  const own   = Object.prototype.hasOwnProperty.call(clock, "onFrame");
  const prev  = clock.onFrame;
  const call  = prev.bind(clock);

  const samples = [];                       // [t, driftMedia]
  const causes  = { seuil: 0, reset: 0, pause: 0 };
  let seen  = clock.stats.resyncs;
  let quiet = 0;

  clock.onFrame = (t) => {
    call(t);
    const s = clock.stats;
    if (s.resyncs !== seen) {
      seen = s.resyncs;
      const c = s.lastResync && s.lastResync.cause;
      if (c in causes) causes[c]++;
      quiet = performance.now() + quietMs;
    }
    if (performance.now() >= quiet && Number.isFinite(s.driftMedia)) {
      samples.push([t, s.driftMedia]);
    }
  };

  const stats = (rows) => {
    if (!rows.length) return { n: 0 };
    const d = rows.map((r) => r[1]);
    const bias = d.reduce((a, b) => a + b, 0) / d.length;
    return {
      n: d.length,
      biais_ms: +(bias * 1000).toFixed(1),
      rms_ms: +(Math.sqrt(d.reduce((a, b) => a + b * b, 0) / d.length) * 1000).toFixed(1),
      max_ms: +(d.reduce((a, b) => Math.max(a, Math.abs(b)), 0) * 1000).toFixed(1),
    };
  };

  const summary = () => {
    // Per ten-second slice as well as overall: it is what said the bias was flat
    // and therefore not an accumulation — the observation that ruled out ever
    // fixing it by raising the threshold.
    const slices = [];
    if (samples.length) {
      const t0 = samples[0][0];
      for (let a = t0; a < samples[samples.length - 1][0]; a += 10) {
        const rows = samples.filter((r) => r[0] >= a && r[0] < a + 10);
        if (rows.length) slices.push({ de_s: +(a - t0).toFixed(0), ...stats(rows) });
      }
    }
    return {
      vitesse: clock.speed,
      taux: { demandé: +clock.stats.rateAsked.toFixed(3),
              obtenu: +clock.stats.rateGot.toFixed(3), refusé: clock.rateRefused },
      décalage_domaine_ms: clock.offsetMeasured ? +(clock.offset * 1000).toFixed(1) : null,
      ...stats(samples),
      recalages: { ...causes },
      tranches: slices,
      visible: document.visibilityState,
    };
  };

  return {
    summary,
    stop() {
      if (own) clock.onFrame = prev; else delete clock.onFrame;
      return summary();
    },
  };
}

/**
 * Le coût du décodeur sur le fps de la scène, aux trois mises en page.
 *
 *     const t = await (await import('/viz/_harness.js')).fps();
 *
 * La mesure que #8 n'a jamais pu faire : `requestAnimationFrame` est **suspendu**
 * dans un onglet caché, donc la boucle Three.js ne tourne pas, le HUD affiche
 * `0 fps` et une campagne lancée là mesure l'économie d'énergie du navigateur.
 * D'où le refus net en tête plutôt qu'un tableau de zéros — un zéro y ressemble
 * exactement à un rendu effondré, et c'est précisément la confusion que le
 * double affichage paquets/s + fps existe pour éviter.
 *
 * À lancer **fenêtre au premier plan**, un replay en cours sur un take *avec
 * vidéo* : sans lecture il n'y a pas de décodeur, donc pas de coût à mesurer.
 * C'est vérifié plutôt que recommandé — un tableau pris sans replay ne dit rien
 * et ne ressemble en rien à un tableau vide. Chaque mise en page est prise
 * séparément parce qu'elles ne coûtent pas la même chose : la superposition
 * compose la 3D par-dessus une vidéo plein cadre, ce qui n'est pas le
 * remplissage d'une incrustation dans un coin.
 *
 * **Le coût est une différence, donc il faut les deux passes.** Le repère se
 * prend sur un take *sans* vidéo — même scène, mêmes mises en page, pas de
 * décodeur — et c'est l'écart qui répond à la question du ticket :
 *
 *     await fps()                       // take avec vidéo, replay en cours
 *     await fps({ repere: true })       // take sans vidéo (001), sans replay
 *
 * Trois colonnes, et la troisième est la seule qui décide. `fps` est **plafonné
 * par le vsync** : deux mises en page à 60 fps ne disent pas laquelle est près de
 * céder. `p95_ms` (l'intervalle entre deux images au 95ᵉ centile) attrape la
 * saccade qu'une moyenne honnête cache. `rendu_ms` est le temps passé *dans*
 * `renderer.render` par image : c'est la marge, et c'est ce qui dit si garder la
 * superposition coûte son prix. Le HUD, lui, moyenne sur une seconde entière.
 */
export async function fps({ seconds = 10, settleS = 1.5, repere = false,
                            vues = [["incrustation", false], ["incrustation", true],
                                    ["cote-a-cote", false], ["superposition", false]],
                          } = {}) {
  const viz = window.__viz;
  if (!viz) throw new Error("scène absente : lancer depuis /viz/");
  if (document.visibilityState !== "visible") {
    throw new Error("onglet caché : rAF est suspendu, la mesure ne vaudrait rien " +
                    "(fenêtre au premier plan, cf. #28)");
  }
  // Le décodeur ne coûte que s'il tourne, et une passe sans lecture serait un
  // repère pris pour une mesure — l'erreur exacte que le tableau doit rendre
  // impossible. Le repère, lui, s'affirme (`repere: true`) et ne se déduit pas.
  if (!repere) {
    const pb = await (await fetch("/api/playback/status")).json();
    const src = document.querySelector(".video-wrap video");
    if (!pb.active) {
      throw new Error("aucune lecture en cours : lance un replay sur un take " +
                      "AVEC vidéo, ou demande le repère (fps({repere:true}))");
    }
    if (!src || !src.currentSrc) {
      throw new Error("le take joué n'a pas de vidéo : sans décodeur il n'y a " +
                      "rien à mesurer — c'est le repère (fps({repere:true}))");
    }
  }

  const before = viz.stats();
  const was    = viz.layout();      // la campagne rend la vue où elle l'a prise
  const rows = [];
  const wait = (s) => new Promise((r) => setTimeout(r, s * 1000));

  const count = (s) => new Promise((resolve) => {
    const gaps = [];
    const a = viz.stats();          // les compteurs de rendu, différenciés
    let last = performance.now();
    const t0 = last;
    let n = 0;
    const step = (now) => {
      gaps.push(now - last); last = now; n++;
      if (now - t0 < s * 1000) requestAnimationFrame(step);
      else {
        const b = viz.stats();
        const frames = b.renderFrames - a.renderFrames;
        gaps.sort((x, y) => x - y);
        resolve({ fps: +(n / ((now - t0) / 1000)).toFixed(1),
                  rendu_ms: frames ? +((b.renderMs - a.renderMs) / frames).toFixed(2) : null,
                  p95_ms: +gaps[Math.floor(gaps.length * 0.95)].toFixed(1),
                  max_ms: +gaps[gaps.length - 1].toFixed(1) });
      }
    };
    requestAnimationFrame(step);
  });

  for (const [name, swapped] of vues) {
    viz.setLayout(name, swapped);
    await wait(settleS);              // le temps que la vidéo reprenne sa place
    const m = await count(seconds);
    const s = viz.stats();
    rows.push({ vue: swapped ? `${name} (permutée)` : name,
                ...m, paquets_hz: s.rateHz, hud_fps: s.fps });
  }

  viz.setLayout(was.layout, was.swapped);
  const meta = { pixelRatio: before.pixelRatio, msaa: before.msaa, dpr: before.dpr,
                 écran: `${screen.width}×${screen.height}`, durée_s: seconds,
                 repère: repere };
  console.table(rows);
  console.log(meta);
  return { vues: rows, rendu: meta };
}

export async function run(mod = "./sync-clock.js", sweepMod = "./sweep.js",
                          layoutMod = "./layout.js") {
  const { VideoSyncClock } = await import(mod + "?h=" + Date.now());
  const { PoseCursor } = await import(sweepMod + "?h=" + Date.now());
  const L = await import(layoutMod + "?h=" + Date.now());
  const log = [];

  // ── Virtual wall clock ─────────────────────────────────────────────────────
  // The clock under test reads `performance.now()` and nothing else, so this is
  // the whole of its notion of real time. Restored in the `finally` below; while
  // the bench runs, the page's own rate HUD reads nonsense for a second.
  const realNow = performance.now.bind(performance);
  let wallMs = 0;
  performance.now = () => wallMs;

  /**
   * A `<video>` reduced to what the sync clock can observe and change.
   *
   * Four behaviours are modelled, each because it produced a defect that was
   * actually paid for:
   *
   *   • `off` — the constant between `currentTime` and `mediaTime`. An MP4 edit
   *     list, or a stream whose first frame is not at zero.
   *   • `stallS` — a seek does not resume playback instantly. This is where the
   *     −67 ms bias came from: the official position moves at once, the picture
   *     restarts late, and nothing ever gives those milliseconds back.
   *   • `frozen` — the browser pausing a muted video in a hidden tab, and
   *     refusing `play()` while it does.
   *   • `maxRate` — the browser silently clamping `playbackRate`.
   */
  class FakeVideo {
    constructor(o = {}) {
      this.duration = o.duration ?? 60;
      this.dt       = o.frameDt ?? 1 / 30;
      // A frame grid deliberately out of phase with round numbers: a probe that
      // landed exactly on a boundary would hide the sub-frame residue the offset
      // measurement necessarily has.
      this.phase    = o.phase ?? 0.007;
      this.off      = o.off ?? 0;
      this.maxRate  = o.maxRate ?? Infinity;
      this.stallS   = o.stallS ?? 0.06;

      this.paused = true;
      this.frozen = false;
      this.seeks  = 0;
      this._ct    = 0;
      this._shown = 0;          // where the *picture* is, which is not `_ct`
      this._stall = 0;
      this._rate  = 1;
    }

    get playbackRate() { return this._rate; }
    set playbackRate(r) { this._rate = Math.min(r, this.maxRate); }

    get currentTime() { return this._ct; }
    // The official position moves the instant it is written — the spec has it
    // set *before* the seek completes, which is exactly why `currentTime` cannot
    // say which frame is on screen.
    set currentTime(t) {
      this._ct = Math.max(0, Math.min(t, this.duration));
      this.seeks++;
      this._stall = this.stallS;
    }

    play() {
      if (this.frozen) return Promise.reject(new Error("paused to save power"));
      this.paused = false;
      return Promise.resolve();
    }
    pause() { this.paused = true; }

    /** Advance by `dt` wall seconds. Returns the PTS if a new frame is shown. */
    tick(dt) {
      const was = this.pts();
      if (this._stall > 0) { this._stall -= dt; return null; }
      if (!this.paused && !this.frozen) {
        this._ct = Math.min(this.duration, this._ct + this._rate * dt);
      }
      this._shown = this._ct;
      const now = this.pts();
      return now === was ? null : now;
    }

    /** The PTS of the displayed frame: quantised, and in the reading domain. */
    pts() {
      return Math.floor((this._shown - this.phase) / this.dt) * this.dt
             + this.phase + this.off;
    }
  }

  /**
   * One take being replayed, with its video beside it.
   *
   * `t` is the model's timeline (`frame.t`) and `wallMs` is real time; the two
   * are related by `speed` and by nothing else, which is what makes a burst of
   * frames expressible at all. The 4 Hz snapshot is repeated the way the panel
   * repeats it, because "the same state, four times a second" is itself
   * something the clock has to survive without acting on it.
   */
  class Bench {
    constructor(o = {}) {
      this.v      = new FakeVideo(o);
      this.clock  = new VideoSyncClock(this.v);
      this.t      = o.t0 ?? 20;
      this.speed  = 1;
      this.rate   = o.modelRateHz ?? 100;
      this.shows  = true;        // false = nothing is ever presented (no rVFC)
      this.active = false;
      this.held   = false;       // paused, as the snapshot reports it
      this.drifts = [];
      this._acc   = 0;
      this._snap  = 0;
      this._seen  = 0;      // seeks already accounted for
      this._quiet = 0;      // wall ms until drift means something again
    }

    /** Open the file: measure the domain offset, then take the anchors. */
    async open(imuS, videoS) {
      const measuring = this.clock.measureOffset();
      // Long enough for the measurement's own short run: it skips the seek's
      // landing frames and then keeps a handful of readings, which is about
      // half a second of presentations at 30 fps.
      await this._run(1.2, false);
      const ok = await measuring;
      this.clock.setAlignment(imuS, videoS);
      // From here on, `seeks` counts resyncs of the pass — what the campaign
      // reports — rather than the one seek the measurement itself costs.
      this.v.seeks = 0;
      return ok;
    }

    play(speed = this.speed, paused = false) {
      this.speed = speed;
      this.active = true;
      this.held = paused;
      this.clock.onPlayback({ active: true, paused, speed });
    }

    /**
     * Run for `wallS` seconds of wall clock, feeding the model's frames.
     *
     * `modelS` overrides how much model time those seconds carry — the one knob
     * a burst needs, and the only way to tell a wall-clock debounce from a
     * replay-time one without waiting for real seconds to pass.
     *
     * Each step yields to the microtask queue, and that is not a detail: the
     * clock re-arms `play()` through a promise, so a synchronous loop leaves it
     * pending forever and the video never restarts. Real frames arrive in
     * separate tasks; a bench that never yields would report a defect this code
     * does not have — and hide the one it does.
     */
    async _run(wallS, feed = true, modelS = null) {
      const step  = 1 / 240;                    // fine enough to resolve a stall
      const steps = Math.max(1, Math.round(wallS / step));
      const dT    = (modelS ?? wallS * this.speed) / steps;
      const grid  = 1 / this.rate;
      for (let i = 0; i < steps; i++) {
        await null;
        wallMs += step * 1000;
        const shown = this.v.tick(step);
        if (shown !== null && this.shows) this.clock.onPresentedFrame(shown);
        // A seek's own transient is not drift, it is the correction: the picture
        // is still the one from before while `currentTime` already names the
        // target, so a sample taken there measures the error the seek just
        // removed. The prototype's campaign excluded it for the same reason —
        // at a loop turnover it is worth fifty seconds and would flatten the
        // ±100 ms signal the whole measurement is about.
        if (this.v.seeks !== this._seen) {
          this._seen  = this.v.seeks;
          this._quiet = wallMs + 300;
        }
        if (!feed) continue;
        if (this.active && (this._snap += step) >= 0.25) {
          this._snap = 0;
          this.clock.onPlayback({ active: true, paused: this.held, speed: this.speed });
        }
        // A paused replay publishes no frame, so its timeline does not advance.
        if (this.held) continue;
        this._acc += dT;
        while (this._acc >= grid) {
          this._acc -= grid;
          this.t += grid;
          this.clock.onFrame(this.t);
          if (wallMs >= this._quiet && Number.isFinite(this.clock.stats.driftMedia)) {
            this.drifts.push(this.clock.stats.driftMedia);
          }
        }
      }
    }

    run(wallS, modelS = null) { return this._run(wallS, true, modelS); }

    /** What the campaign reports, computed the same way here. */
    summary() {
      const d = this.drifts;
      if (!d.length) return { n: 0, bias: null, rms: null, max: null };
      const bias = d.reduce((a, b) => a + b, 0) / d.length;
      const rms  = Math.sqrt(d.reduce((a, b) => a + b * b, 0) / d.length);
      const max  = d.reduce((a, b) => Math.max(a, Math.abs(b)), 0);
      return { n: d.length, bias, rms, max };
    }

    clear() { this.drifts.length = 0; }
  }

  const ms = (x) => (x === null || x === undefined ? null : +(x * 1000).toFixed(1));
  const say = (cas, ok, lu, attendu) => log.push({ cas, ok, lu, attendu });

  /**
   * A pose track being computed, served the way the endpoint serves one.
   *
   * `limitS` is the take time the computation has reached and is *moved* by the
   * cases: the interesting state is not a finished track but one whose limit is
   * still walking forward under the cursor. `asked` records every URL, which is
   * how "one request per chunk" is asserted at all — a cursor that refetched on
   * every pointer event would behave identically to the eye and hammer the loop
   * this page is careful not to saturate.
   */
  class FakeTrack {
    constructor({ hz = 100, takeS = 60, limitS = 60, noPosition = false } = {}) {
      this.hz = hz; this.takeS = takeS; this.limitS = limitS;
      this.noPosition = noPosition;
      this.asked = [];
    }

    fetch = async (url) => {
      this.asked.push(url);
      const q = new URLSearchParams(url.split("?")[1]);
      const points = +(q.get("points") || 0);
      const start  = q.has("start") ? +q.get("start") : 0;
      const end    = q.has("end")   ? +q.get("end")   : this.limitS;
      const cols   = { t: [], qw: [], qx: [], qy: [], qz: [], x: [], y: [], z: [] };
      if (!points) {
        for (let k = Math.ceil(start * this.hz); k / this.hz <= Math.min(end, this.limitS); k++) {
          const t = k / this.hz;
          cols.t.push(t);
          cols.qw.push(1); cols.qx.push(0); cols.qy.push(0); cols.qz.push(t);
          // A take recorded without a gyro has no horizontal position at all,
          // and the endpoint says so with null — never with a zero.
          cols.x.push(this.noPosition ? null : t);
          cols.y.push(this.noPosition ? null : -t);
          cols.z.push(1);
        }
      }
      return {
        status: this.limitS >= this.takeS ? "ready" : "computing",
        records: Math.round(this.limitS * this.hz),
        duration_s: this.limitS,
        complete: this.limitS >= this.takeS,
        geometry: { R_TORE: 1, r_TORE: 0.05,
                    current: { R_TORE: 1, r_TORE: 0.05 }, matches: true },
        error: null,
        poses: cols,
      };
    };
  }

  // Everything the cursor does is a promise away; nothing here waits on time.
  const settle = async (n = 6) => { for (let i = 0; i < n; i++) await null; };

  try {
    // ── The two domains ──────────────────────────────────────────────────────
    // The whole reason this bench exists. A clock that ignores the offset, or
    // one that adds it instead of subtracting it, follows the movement exactly —
    // and sits a fixed number of frames beside the mark. Nothing else on the
    // page would ever say so.
    for (const off of [0.5, -0.05]) {
      const b = new Bench({ off, duration: 60 });
      const measured = await b.open(20, 22);
      b.play(1);
      await b.run(6);
      const s = b.summary();
      // One frame of tolerance: a seek to T shows the frame *containing* T, so
      // the measurement falls short of the true constant by up to one interval.
      const ok = measured && Math.abs(s.bias) < 0.05 && s.max < 0.1;
      say(`décalage de domaine ${off > 0 ? "+" : ""}${off} s : converti, jamais additionné`,
          ok, { décalage: +b.clock.offset.toFixed(3), biais: ms(s.bias), max: ms(s.max) },
          { décalage: off, biais: "|·| < 50 ms", max: "< 100 ms" });
    }

    // The measurement is a fact about the file, so it has to *happen*: a clock
    // that assumed zero would pass every drift test ever written.
    {
      const b = new Bench({ off: 0.5 });
      const ok = await b.open(20, 22);
      say("le décalage est mesuré à l'ouverture, pas supposé",
          ok && b.clock.offsetMeasured && Math.abs(b.clock.offset - 0.5) < 0.04,
          { mesuré: b.clock.offsetMeasured, décalage: +b.clock.offset.toFixed(3) },
          { mesuré: true, décalage: 0.5 });
    }

    // Where nothing is ever presented — no rVFC, or a hidden document — there is
    // no second domain to keep apart. Reporting "measured 0" would be a lie in
    // the one shape that cannot be told from the truth.
    {
      const b = new Bench({ off: 0 });
      b.shows = false;
      const ok = await b.open(20, 22);
      say("sans présentation : rien n'est mesuré, et ça se sait",
          ok === false && b.clock.offsetMeasured === false && b.clock.offset === 0,
          { mesuré: b.clock.offsetMeasured, décalage: b.clock.offset },
          { mesuré: false, décalage: 0 });
    }

    // ── Trim, resync, and what each is for ───────────────────────────────────
    // A seek installs a constant lag (playback restarts late) and no threshold
    // ever removes it — raising the threshold trades a jolt for lag, forever.
    // The trim removes it, and that is all it has to do.
    {
      const b = new Bench({ stallS: 0.12 });
      await b.open(20, 22);
      b.play(1);
      await b.run(1);                       // the opening hard sync, and its stall
      const afterSync = b.v.seeks;
      b.clear();
      await b.run(8);
      const s = b.summary();
      say("le trim résorbe un retard constant sans jamais recaler",
          Math.abs(s.bias) < 0.05 && b.v.seeks === afterSync,
          { biais: ms(s.bias), rms: ms(s.rms), recalages: b.v.seeks - afterSync },
          { biais: "|·| < 50 ms", recalages: 0 });
    }

    // Chrome pauses a muted video in a hidden tab and refuses `play()` while it
    // does. The trim is capped at ±10 %, so it would need fifteen seconds to
    // recover 1.5 s: the hard resync is the net for the moment the browser stops
    // the video under our feet.
    {
      const b = new Bench({});
      await b.open(20, 22);
      b.play(1);
      await b.run(2);
      b.v.frozen = true;
      b.v.paused = true;              // the tab goes away
      await b.run(1.5);
      const during = b.v.seeks;
      b.v.frozen = false;
      await b.run(2);                 // …se recale…
      const recovered = !b.v.paused && Math.abs(b.clock.stats.driftMedia) < 0.15;
      b.clear();
      await b.run(2);                 // …et repart
      const s = b.summary();
      // Two facts, not one: that it came back, and that it then runs clean. A
      // mean taken across the recovery itself measures the recovery, and would
      // pass just as well on a version that came back slowly to the wrong place.
      say("onglet caché : la vidéo se recale et repart",
          recovered && Math.abs(s.bias) < 0.1 && s.max < 0.25,
          { recalée: recovered, biais_ensuite: ms(s.bias), max_ensuite: ms(s.max),
            recalages_pendant: during },
          { recalée: true, biais_ensuite: "|·| < 100 ms", max_ensuite: "< 250 ms" });
    }

    // The debounce caps a *cost* — main thread, decoder — and a cost is paid per
    // second of wall. Five seconds of model time inside a third of a second of
    // wall: counted in replay time that is twenty crossings, counted in wall time
    // at most two.
    {
      const b = new Bench({});
      await b.open(20, 22);
      b.play(1);
      await b.run(1);
      const before = b.v.seeks;
      b.v.frozen = true;              // a lasting error: every frame wants a seek
      b.v.paused = true;
      await b.run(0.3, 5);                  // 5 s of model time in 0.3 s of wall
      const seeks = b.v.seeks - before;
      say("l'anti-rebond se compte en secondes de mur, pas de replay",
          seeks <= 2, { recalages: seeks, mur_s: 0.3, replay_s: 5 },
          { recalages: "≤ 2 (0,3 s / 0,25 s)" });
    }

    // ── What must not be written ─────────────────────────────────────────────
    {
      const b = new Bench({ stallS: 0.02 });
      await b.open(20, 22);
      b.play(1);
      await b.run(10);
      say("on n'écrit pas currentTime à chaque frame", b.v.seeks === 1,
          { recalages: b.v.seeks, frames: Math.round(10 * b.rate) },
          { recalages: 1 });
    }

    // The two instants where the timeline changes direction, and the only two
    // where a hard seek is the right behaviour rather than a fallback.
    {
      const b = new Bench({});
      await b.open(20, 22);
      b.play(1);
      await b.run(2);
      const before = b.v.seeks;
      b.t = 0;                        // a loop turns over: the take restarts
      b.clock.onReset();
      await b.run(1);
      say("chaque tour de boucle recale", b.v.seeks - before === 1,
          { recalages: b.v.seeks - before }, { recalages: 1 });
    }
    {
      const b = new Bench({});
      await b.open(20, 22);
      b.play(1);
      await b.run(2);
      const running = b.v.seeks;
      b.play(1, true);                // pause: the picture is posed once…
      await b.run(1);                       // …and the 4 Hz snapshot repeats meanwhile
      const paused = b.v.seeks - running;
      b.play(1, false);
      await b.run(1);
      say("une pause pose l'image une fois, une reprise recale",
          paused === 1 && b.v.seeks - running - paused === 1,
          { pendant_la_pause: paused, à_la_reprise: b.v.seeks - running - paused },
          { pendant_la_pause: 1, à_la_reprise: 1 });
    }

    // ── Falling silent ───────────────────────────────────────────────────────
    // 8.8 s of the reference take's 58.9 have no picture. Showing the first frame
    // as though it were right, and going on reporting a drift against it, are the
    // same mistake twice.
    //
    // Both ends, because they are two different facts about a take and the page
    // says which one it is: the reference session has one take whose camera
    // started after it (anchors 19.7 / 22.6, so the tail runs past the file) and
    // the opposite happens as soon as the camera is stopped first.
    for (const [name, imu, vid, t0, dur, état] of [
      ["avant", 20, 5, 2, 60, "avant la vidéo"],
      // The reference take's own numbers: 58.9 s of IMU against 50.1 s of video.
      ["après", 19.7, 22.6, 50, 50.1, "après la vidéo"],
    ]) {
      const b = new Bench({ t0, duration: dur });
      await b.open(imu, vid);
      b.play(1);
      await b.run(2);
      const c = b.clock.stats;
      say(`hors plage (${name}) : la page se tait et cesse de mesurer une dérive`,
          c.outOfRange && c.drift === null && c.driftMedia === null && b.v.paused,
          { état: c.state, dérive: c.drift, en_pause: b.v.paused },
          { état, dérive: null, en_pause: true });
    }

    // Past the ceiling the trim stays pinned and the resyncs become one
    // continuous jolt (measured at ×2: 54 in 22 s). Freezing honestly beats that.
    {
      const b = new Bench({});
      await b.open(20, 22);
      b.play(4);
      await b.run(3);
      const c = b.clock.stats;
      const detached = c.detached && b.v.paused && c.drift === null && b.v.seeks === 0;
      b.play(1);                      // and it rejoins when the speed comes back
      await b.run(1.5);
      const back = !b.clock.stats.detached
                   && Math.abs(b.clock.stats.driftMedia) < 0.1;
      say("au-delà du plafond la vidéo se détache, et revient quand la vitesse baisse",
          detached && back,
          { détachée_à_4: detached, dérive_de_retour: ms(b.clock.stats.driftMedia) },
          { détachée_à_4: true, dérive_de_retour: "|·| < 100 ms" });
    }

    // A rate the browser refuses in silence would make the video diverge with
    // nothing to say so.
    {
      // At a speed that still follows — past the detach threshold nothing is
      // written to the element at all, and the case would pass for the wrong
      // reason.
      const b = new Bench({ maxRate: 1.2 });
      await b.open(20, 22);
      b.play(1.5);
      await b.run(1);
      say("un taux de lecture refusé est visible", b.clock.rateRefused,
          { demandé: +b.clock.stats.rateAsked.toFixed(2),
            obtenu: +b.clock.stats.rateGot.toFixed(2), signalé: b.clock.rateRefused },
          { signalé: true });
    }

    // The slow motion is the case the map calls the most precious, and the one
    // the campaign found best: nothing here may be tuned for ×1 alone.
    {
      const b = new Bench({});
      await b.open(20, 22);
      b.play(0.25);
      await b.run(2);
      b.clear();
      await b.run(8);
      const s = b.summary();
      say("au ralenti (×0,25) la vidéo suit sans recaler",
          Math.abs(s.bias) < 0.05 && b.v.seeks === 1,
          { biais: ms(s.bias), rms: ms(s.rms), recalages: b.v.seeks },
          { biais: "|·| < 50 ms", recalages: 1 });
    }

    // ── Le balayage : la main, pas le replay (#29) ────────────────────────────
    // Sweeping drives the same element from a cursor instead of from `frame.t`,
    // and the conversion between the two domains is the same one — which is the
    // point: a scrub that wrote the target as though it were a request would sit
    // a fixed number of frames beside the mark, silently, exactly like the
    // playing path before this bench existed.
    {
      const b = new Bench({ off: 0.5, duration: 60 });
      await b.open(20, 22);            // t − 20 + 22 in the reading domain
      b.clock.scrub(30);
      b.clock.endScrub();
      const want = 30 - 20 + 22 - b.clock.offset;
      say("le curseur pose l'image, décalage de domaine converti",
          Math.abs(b.v.currentTime - want) < 0.05 && b.v.paused,
          { position: +b.v.currentTime.toFixed(3), en_pause: b.v.paused },
          { position: +want.toFixed(3), en_pause: true });
    }

    // A measurement borrows the element for half a second — it seeks, it plays.
    // A sweep is entitled to ask for one it had given up (the cursor stopped the
    // element under it), so the measurement has to give the position back, or
    // letting go would leave the picture at the probe's own second, seconds from
    // where the hand stopped and looking exactly like a wrong alignment.
    {
      const b = new Bench({ off: 0.5, duration: 60 });
      b.clock.setAlignment(20, 22);
      b.clock.scrub(35);
      b.clock.endScrub();
      const posed = b.v.currentTime;
      const measuring = b.clock.measureOffset();
      await b._run(1.2, false);
      const ok = await measuring;
      say("une mesure du décalage rend l'image où elle l'a prise",
          ok && Math.abs(b.v.currentTime - posed) < 1e-3 && b.v.paused,
          { avant: +posed.toFixed(3), après: +b.v.currentTime.toFixed(3),
            mesurée: ok },
          { après: +posed.toFixed(3), mesurée: true });
    }

    // "Chercher n'est pas jouer" is a property of the element too: whatever the
    // replay is doing, a hand on the cursor is the one driver until it lets go.
    {
      const b = new Bench({});
      await b.open(20, 22);
      b.play(1);
      await b.run(1);
      b.clock.scrub(40);
      const posed = b.v.currentTime;
      const seeks = b.v.seeks;
      // The hand is still down: the replay was never stopped, so its frames and
      // its 4 Hz snapshots keep arriving. Neither may touch the element.
      await b.run(0.5);
      const held = Math.abs(b.v.currentTime - posed) < 1e-9
                   && b.v.seeks === seeks && b.v.paused;
      b.clock.endScrub();
      say("pendant le balayage, les frames du replay ne reprennent pas la main",
          held,
          { bougé_ms: ms(b.v.currentTime - posed), écritures: b.v.seeks - seeks,
            en_pause: b.v.paused },
          { bougé_ms: 0, écritures: 0, en_pause: true });
    }

    // A drag produces one request per pointer event — sixty a second — and each
    // one is a decode restart. The debounce is the same wall-clock budget the
    // resync obeys, and the *last* position asked for is honoured exactly, or
    // the picture would stop a fraction of a second short of where the hand is.
    {
      const b = new Bench({});
      await b.open(20, 22);
      const before = b.v.seeks;
      for (let i = 0; i < 60; i++) {
        b.clock.scrub(30 + i * 0.1);
        await b.run(0.5 / 60, 0);      // 0.5 s of wall for the whole drag
      }
      b.clock.endScrub();
      const want = 30 + 59 * 0.1 - 20 + 22;
      say("une main sur le curseur ne noie pas le décodeur, et finit juste",
          b.v.seeks - before <= 8 && Math.abs(b.v.currentTime - want) < 0.02,
          { écritures: b.v.seeks - before, requêtes: 60,
            arrivée: +b.v.currentTime.toFixed(3) },
          { écritures: "≤ 8 (0,5 s / 80 ms)", arrivée: +want.toFixed(3) });
    }

    // Out of range while sweeping is the ordinary case, not an edge one — 8.8 s
    // of the reference take's 58.9 have no picture. Writing a bounded position
    // would show a frame from elsewhere as though it were this instant's.
    {
      const b = new Bench({ duration: 60 });
      await b.open(20, 5);             // the take starts long after the video
      const before = b.v.seeks;
      b.clock.scrub(2);                // → −13 s in the video
      b.clock.endScrub();
      const c = b.clock.stats;
      say("balayage hors plage : rien n'est écrit, et la page le dit",
          b.v.seeks === before && c.outOfRange && c.state === "avant la vidéo",
          { écritures: b.v.seeks - before, état: c.state },
          { écritures: 0, état: "avant la vidéo" });
    }

    // ── Le curseur sur la piste de pose (#29) ────────────────────────────────
    // The pose under the cursor is *at or before* it, never the nearest: a pose
    // is a point the wheel actually passed through, and rounding forward hands
    // back a position it had not reached yet. Same rule as `read_pose_at`.
    {
      const track  = new FakeTrack({ hz: 100 });
      const cursor = new PoseCursor({ fetchJson: track.fetch, pollMs: 1e9 });
      cursor.open("s", "t");
      cursor.poseAt(12.345);
      await settle();
      const p = cursor.poseAt(12.345);
      say("le curseur rend la pose à ou avant l'instant",
          !!p && Math.abs(p.t - 12.34) < 1e-9 && Math.abs(p.x - 12.34) < 1e-9,
          { t: p && +p.t.toFixed(3) }, { t: 12.34 });
    }

    // A drag fires one of these per pointer event — fifty for the second below.
    // Refetching a stretch already held would be invisible to the eye and would
    // hammer the very loop this page takes care not to saturate. The chunk the
    // hand is heading for has to be there *before* it arrives, or the picture
    // stops at every boundary; the one behind, for the same reason, since a
    // hand goes back as readily as forward.
    {
      const track  = new FakeTrack({ hz: 100 });
      const cursor = new PoseCursor({ fetchJson: track.fetch, pollMs: 1e9 });
      cursor.open("s", "t");
      await settle();
      const before = track.asked.length;
      for (let t = 12; t < 13; t += 0.02) { cursor.poseAt(t); await settle(2); }
      const forOneSecond = track.asked.length - before;   // 50 pointer events
      for (let t = 17; t < 18.2; t += 0.02) { cursor.poseAt(t); await settle(2); }
      const asked = track.asked.filter((u) => u.includes("start="));
      const spans = new Set(asked.map((u) => u.match(/start=(\d+)/)[1]));
      say("un tronçon n'est demandé qu'une fois, et le voisin avant d'y arriver",
          asked.length === spans.size && forOneSecond <= 2 && spans.has("20"),
          { requêtes: asked.length, tronçons: [...spans].sort().join(","),
            pour_une_seconde: forOneSecond, gestes: 50 },
          { requêtes: "= nombre de tronçons (3)", tronçons: "0,10,20",
            pour_une_seconde: "≤ 2" });
    }

    // A take opened the moment it was recorded: the computation is behind the
    // hand rather than ahead of it. The cursor stops at the limit — cleanly,
    // since past it there is nothing to draw — and follows it forward when the
    // computation catches up. Nothing here is asked for by hand: only the reply
    // says where the limit is, so only a fresh reply can move it.
    {
      const track  = new FakeTrack({ takeS: 60, limitS: 20 });
      const cursor = new PoseCursor({ fetchJson: track.fetch, pollMs: 1e9 });
      cursor.open("s", "t");
      await settle();
      const stopped = cursor.clamp(45);
      const partial = !cursor.complete && cursor.limitS === 20;
      track.limitS = 60;                 // the computation gets there
      // The limit has to move on the *poll* alone, before anything is asked for
      // around the cursor: it is the one change no request of ours would reveal,
      // and a hand parked past the limit makes no request at all.
      await cursor.refresh();
      await settle();
      const followed = cursor.limitS === 60 && cursor.complete
                       && cursor.clamp(45) === 45;
      cursor.poseAt(45);
      await settle();
      const p = cursor.poseAt(45);
      say("piste tronquée : le curseur s'arrête à la limite, puis la suit",
          partial && stopped === 20 && followed && !!p && Math.abs(p.t - 45) < 1e-9,
          { limite_avant: 20, curseur_borné: stopped, limite_après: cursor.limitS,
            suivie_au_sondage: followed, pose_à_45: !!p },
          { curseur_borné: 20, limite_après: 60, suivie_au_sondage: true,
            pose_à_45: true });
    }

    // A wheel recorded without a gyro has no horizontal position at all. Null
    // has to survive the whole way to the renderer: a zero there draws it
    // sitting at the origin, which is a plausible, wrong fact.
    {
      const track  = new FakeTrack({ noPosition: true });
      const cursor = new PoseCursor({ fetchJson: track.fetch, pollMs: 1e9 });
      cursor.open("s", "t");
      cursor.poseAt(5);
      await settle();
      const p = cursor.poseAt(5);
      say("une position absente reste absente, jamais un zéro",
          !!p && p.x === null && p.y === null && p.qw === 1,
          { x: p && p.x, y: p && p.y }, { x: null, y: null });
    }

    // Sweeping a fifteen-minute take must not end with the whole track in
    // memory — that is precisely what the chunking is for.
    {
      const track  = new FakeTrack({ takeS: 900, limitS: 900 });
      const cursor = new PoseCursor({ fetchJson: track.fetch, keep: 4, pollMs: 1e9 });
      cursor.open("s", "t");
      for (let t = 0; t < 300; t += 5) { cursor.poseAt(t); await settle(2); }
      say("le curseur ne garde qu'une fenêtre du take en mémoire",
          cursor._chunks.size <= 4,
          { tronçons_gardés: cursor._chunks.size }, { tronçons_gardés: "≤ 4" });
    }

    // Letting go of a running replay: the picture is seconds away from where the
    // replay resumes, and that is precisely the moment a hard seek is right
    // rather than a fallback — the same instant a reset is.
    {
      const b = new Bench({});
      await b.open(20, 22);
      b.play(1);
      await b.run(1);
      b.clock.scrub(40);
      b.clock.endScrub();
      const before = b.v.seeks;
      b.t = 40;                        // the replay resumes where the cursor was
      await b.run(1.5);
      const s = b.clock.stats;
      say("la main relâchée, la lecture reprend la main et se recale une fois",
          b.v.seeks - before === 1 && !b.v.paused && s.state === "suit le replay"
          && Math.abs(s.driftMedia) < 0.1,
          { recalages: b.v.seeks - before, état: s.state,
            dérive: ms(s.driftMedia) },
          { recalages: 1, état: "suit le replay", dérive: "|·| < 100 ms" });
    }

    // ── Les trois mises en page, et ce que la scène en dit ───────────────────
    // Rien ici n'est asynchrone ni ne touche au DOM : `describe()` est la seule
    // décision, et les deux appliquants n'en prennent aucune. C'est ce qui rend
    // ces règles vérifiables — appliquées à la main dans deux fichiers, elles
    // seraient d'accord jusqu'au jour où l'un des deux changerait.
    {
      // Un `localStorage` de bench : le vrai n'est pas disponible partout (la
      // navigation privée fait lever l'accès) et le vider casserait la page.
      const store = (init = {}) => {
        const m = new Map(Object.entries(init));
        return { getItem: (k) => (m.has(k) ? m.get(k) : null),
                 setItem: (k, v) => m.set(k, String(v)), _m: m };
      };

      {
        const s = store();
        L.saveLayout(s, "superposition", true);
        const read = L.readLayout(s);      // le rechargement, sans recharger
        say("le choix de mise en page survit à un rechargement",
            read.layout === "superposition" && read.swapped === true,
            read, { layout: "superposition", swapped: true });
      }
      {
        const read = L.readLayout(store({ "viz.layout": "plein-ecran" }));
        say("une mise en page inconnue retombe sur l'incrustation",
            read.layout === "incrustation", read, { layout: "incrustation" });
      }

      // Le volet replié : sans lui, un take sans vidéo coûte la moitié de
      // l'écran pour un cadre vide.
      {
        const d = L.describe({ layout: "cote-a-cote", hasTake: true, hasVideo: false });
        say("sans vidéo, le côte-à-côte replie son volet",
            d.video === "folded" && d.scene === "full" && !d.hatched,
            { volet: d.video, scène: d.scene }, { volet: "folded", scène: "full" });
      }
      // Ailleurs le cadre reste — hachuré, et sans badge : l'absence est déjà
      // nommée, et un badge d'état sur une absence est un état de trop.
      {
        const rows = ["incrustation", "superposition"].map((layout) =>
          L.describe({ layout, hasTake: true, hasVideo: false,
                       driven: true, state: "non aligné" }));
        // En superposition le cadre est *derrière* la scène : un sol opaque le
        // cacherait entièrement, et l'absence ne se verrait nulle part.
        const vu = rows[1].transparent;
        say("sans vidéo : cadre hachuré ailleurs, visible, et jamais de badge",
            rows.every((d) => d.hatched && d.badge === "" && d.video !== "folded") && vu,
            rows.map((d) => ({ cadre: d.video, hachuré: d.hatched, badge: d.badge,
                               transparent: d.transparent })),
            { hachuré: true, badge: "", "transparent (superposition)": true });
      }

      // Aucun take à l'écran — le direct, ou la page qui vient d'ouvrir. Il n'y
      // a pas d'image absente à signaler : il n'y a pas de take. Un cadre
      // hachuré là serait une réponse à une question que personne n'a posée, et
      // en superposition il coûterait le sol et la grille de la scène.
      {
        const rows = L.LAYOUTS.map((layout) => L.describe({ layout }));
        say("sans take : aucun cadre, et la scène garde son sol et son suivi",
            rows.every((d) => d.video === "folded" && !d.hatched
                              && !d.transparent && d.follow),
            rows.map((d) => ({ vue: d.layout, cadre: d.video, hachuré: d.hatched,
                               transparent: d.transparent, suivi: d.follow })),
            { cadre: "folded", hachuré: false, transparent: false, suivi: true });
      }

      // Les trois badges du ticket, sur le bandeau de scène et image grisée.
      {
        const rows = ["non aligné", "avant la vidéo", "après la vidéo"].map((state) =>
          L.describe({ layout: "incrustation", hasVideo: true, driven: true, state }));
        say("non aligné / avant / après la vidéo : badge et image grisée",
            rows.every((d, i) => d.badge === ["non aligné", "avant la vidéo",
                                              "après la vidéo"][i] && d.greyed),
            rows.map((d) => ({ badge: d.badge, grisée: d.greyed })),
            { badge: "l'état", grisée: true });
      }
      {
        // Au-delà du seuil de vitesse l'image ne peut plus suivre : le dire vaut
        // mieux que de la laisser sauter en donnant à croire que c'est le take.
        const d = L.describe({ layout: "cote-a-cote", hasVideo: true, driven: true,
                               state: "décrochée (×2)" });
        say("le décrochage est annoncé à l'écran",
            d.badge === "décrochée (×2)" && d.greyed,
            { badge: d.badge, grisée: d.greyed },
            { badge: "décrochée (×2)", grisée: true });
      }
      {
        // Trois états où l'image affichée est *juste*, et qui ne doivent donc
        // rien dire : la pause pose la frame de l'instant et la tient — la
        // griser retournerait exactement le raisonnement qui fait griser les
        // autres.
        const q = (state, driven = true) =>
          L.describe({ layout: "incrustation", hasVideo: true, driven, state });
        const suit  = q("suit le replay");
        const pause = q("en pause");
        const idle  = q("inactif", false);
        say("le nominal, la pause et l'inactivité ne portent pas de badge",
            [suit, pause, idle].every((d) => d.badge === "" && !d.greyed),
            { suit: suit.badge, pause: [pause.badge, pause.greyed], inactif: idle.badge },
            { suit: "", pause: ["", false], inactif: "" });
      }
      {
        const d = L.describe({ layout: "incrustation", hasVideo: true,
                               driven: true, state: "balayage" });
        say("le balayage est nommé mais pas grisé",
            d.badge === "balayage" && !d.greyed,
            { badge: d.badge, grisée: d.greyed }, { badge: "balayage", grisée: false });
      }

      // La superposition : vue suggestive, pas vérification. La caméra virtuelle
      // n'est pas posée comme la vraie et rien ne l'enregistre, donc son suivi de
      // roue est coupé — sinon l'image de synthèse glisse sur une image filmée
      // qui, elle, ne bouge pas.
      {
        const sup = L.describe({ layout: "superposition", hasVideo: true });
        const aut = ["incrustation", "cote-a-cote"].map((layout) =>
          L.describe({ layout, hasVideo: true }));
        say("la superposition coupe le suivi de roue et compose sur la vidéo",
            !sup.follow && sup.transparent && sup.video === "full"
            && aut.every((d) => d.follow && !d.transparent),
            { superposition: { suivi: sup.follow, transparent: sup.transparent },
              autres: aut.map((d) => ({ suivi: d.follow, transparent: d.transparent })) },
            { superposition: { suivi: false, transparent: true },
              autres: "suivi conservé" });
      }
      {
        const inc = L.describe({ layout: "incrustation", hasVideo: true, swapped: true });
        const cot = L.describe({ layout: "cote-a-cote", hasVideo: true, swapped: true });
        const sup = L.describe({ layout: "superposition", hasVideo: true, swapped: true });
        say("la permutation n'appartient qu'à l'incrustation",
            inc.video === "full" && inc.scene === "inset"
            && cot.video === "half" && cot.scene === "half"
            && sup.video === "full" && sup.scene === "full",
            { incrustation: [inc.video, inc.scene], côte: [cot.video, cot.scene],
              superposition: [sup.video, sup.scene] },
            { incrustation: ["full", "inset"], côte: ["half", "half"],
              superposition: ["full", "full"] });
      }
    }
  } finally {
    performance.now = realNow;
  }

  const bad = log.filter((r) => !r.ok).length;
  console.table(log);
  console.log(bad ? `${bad}/${log.length} CAS ROUGES` : `${log.length} cas verts`);
  return log;
}
