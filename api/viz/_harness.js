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

export async function run(mod = "./sync-clock.js") {
  const { VideoSyncClock } = await import(mod + "?h=" + Date.now());
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
  } finally {
    performance.now = realNow;
  }

  const bad = log.filter((r) => !r.ok).length;
  console.table(log);
  console.log(bad ? `${bad}/${log.length} CAS ROUGES` : `${log.length} cas verts`);
  return log;
}
