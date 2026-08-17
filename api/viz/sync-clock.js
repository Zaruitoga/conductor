// sync-clock.js — the video, slave to the replay's own timeline.
//
// Lifted from `proto/8-video-dans-le-viz` (the one piece of that prototype worth
// keeping) with the correction its measurement campaign produced. It knows
// nothing of layout, WebSockets or Three.js: it receives numbers and drives a
// `<video>`.
//
// The contract, in one calculation — both anchors, never their difference:
//
//     target = frame.t − onset_imu_s + onset_video_s      (then a domain conversion)
//
// `frame.t` and never `playback.elapsed_s`: the snapshot is 4 Hz rounded to a
// tenth, which is 72° of wheel at two turns a second.
//
// Two domains, never added
// ------------------------
// Requests are written in `currentTime` (`ct`), readings come back as
// `mediaTime` (`media`), and nothing guarantees the two share an origin — an MP4
// edit list installs a constant between them. Both anchors are read from what
// the page *displayed* (`/align/` writes the PTS of the designated frame), so a
// target is a `media` instant and has to be converted before it can be written.
// Get that wrong and the failure is silent: the video follows the movement
// perfectly, offset by a fixed number of frames, which no drift measurement
// catches because the drift stays zero.
//
// The offset is measured when the file opens and stored nowhere (decision #19).
// It is a property of the *file*, not of the alignment — storing it would make
// it lie the day the video is re-encoded without the alignment changing, and
// would reopen "both anchors, never their difference" through a third derived
// value. How it is measured moved (see `measureOffset`): the prescribed single
// reading after a seek turned out to measure the *seek's landing* on the
// reference rushes, and applying that as a constant shifted the picture by 65 ms
// — the very failure the conversion is there to prevent.
//
// Trim and hard resync, both
// --------------------------
// The prototype's campaign, over a full pass of the reference take at ×1:
//
//   hard resync, 100 ms (the original design) │ −67 ms bias │ 70 ms rms │ 7 resyncs
//   hard resync, 250 ms                       │ −112 ms     │ 113 ms    │ 0
//   trim alone                                │  −14 ms     │  24 ms    │ 0
//   trim + hard resync 250 ms                 │  −15 ms     │  21 ms    │ 0
//
// Not a trade-off but a dominance. The bias is flat across the pass, so it is
// not an accumulation: both clocks run at the same rate (`frame.t` is within
// 0.02 % of wall time — re-measured here at 1.0003 — and a video left free
// within 0.017 %, re-measured at 1.0001). It installs itself at the first seek,
// while playback restarts, and nothing ever reabsorbs it. Raising the threshold
// trades a jolt for lag, indefinitely; the trim cancels a constant offset
// without ever jumping.
//
// The hard resync stays mandatory *despite* the trim: Chrome pauses a muted
// video in a hidden tab, and a capped trim never recovers a second and a half of
// lag (measured: −1.5 s after 40 s hidden). The net is there for the moment the
// browser stops the video under our feet.

// How many seconds we give ourselves to reabsorb a drift by playing slightly
// off-speed, and how far off-speed we are willing to go. Past that it stops
// being a catch-up and becomes a slow motion of its own.
const TRIM_TAU_S = 1.0;
const TRIM_MAX   = 0.10;

// Both decisions are taken on a **smoothed** drift, never on one sample.
//
// Measured on the reference take, foreground window: `frame.t` arrives with an
// rms jitter of 69 ms against the wall clock (p95 140 ms, one interval in a
// hundred over 179 ms, worst 506 ms) while its *slope* is 1.0002 — the two
// clocks agree, the arrivals do not. A decoder cannot follow that, and it should
// not try: taken sample by sample, the threshold fires on the machine's
// scheduling rather than on the video (measured: ten hard resyncs in 30 s, each
// correcting an error that was gone by the next frame), and the trim writes
// `playbackRate` thirty times a second for the same reason.
//
// A second is short against a genuine desynchronisation — a video the browser
// has stopped falls behind a second per second and still crosses the threshold
// in about a second, verified on the page — and long against the arrivals. The
// same ×1 pass, same machine: no smoothing 10 resyncs / 30 s, τ = 0.5 s 8 / 50 s,
// τ = 1 s **0** — with the rms falling 70 → 47 ms and the rate writes 27/s → 8/s.
const DRIFT_TAU_S = 1.0;

// Past this, a seek is the only thing that closes the gap — the browser paused
// the video behind our back, or the replay jumped. 100 ms was the worst possible
// value, measured: it sits just above the natural bias, so jitter crosses it
// seven times a pass and each crossing is a jolt that corrects nothing lasting.
const HARD_RESYNC_S = 0.25;

// A seek writes `currentTime`; the official position moves at once but the
// picture takes a GOP to follow, so asking for another straight away buys
// nothing. **Counted in wall-clock seconds, not replay seconds** — what it caps
// is a *cost* (main thread, decoder), and a cost is paid per second of wall,
// exactly like the OSC bridge's send cadence. Counted in replay time the same
// 0.25 s was worth 62 ms of wall at ×4: nine jolts a real second, on the very
// budget this page caps on purpose.
const SEEK_COOLDOWN_S = 0.25;

// The same budget, for the hand instead of the replay. A drag fires one request
// per pointer event — sixty a second — and each one is a decode restart, so the
// picture is moved on a cadence and the position asked for in between is kept,
// latest wins (`endScrub` honours the last one exactly). 80 ms is twelve
// pictures a second: continuous to the eye, and a fraction of what a drag would
// otherwise ask of the decoder. It is deliberately shorter than the resync's
// debounce — a hand expects the picture to follow it, where a resync is a
// correction nobody asked for.
const SCRUB_COOLDOWN_S = 0.08;

// Past this replay speed the video **detaches**: it stops following and freezes.
// Falling silent honestly beats a jolt that corrects nothing — and past here it
// is nothing else. Measured on the reference take, foreground window, one line
// per pass (drift = the PTS actually presented, against the target):
//
//   ×0,25 │ −31 ms │  33 ms rms │  94 ms max │  0 resync   │ arrivals   2 ms rms
//   ×1    │ −17 ms │  39 ms rms │ 155 ms max │  0          │           23 ms rms
//   ×1    │ −65 ms │  82 ms rms │ 299 ms max │  0          │           43 ms rms
//   ×1,25 │ −22 ms │  54 ms rms │ 274 ms max │  0          │           39 ms rms
//   ×1,5  │ −28 ms │  72 ms rms │ 253 ms max │  0          │           56 ms rms
//   ×1,5  │ −37 ms │  67 ms rms │ 221 ms max │  0          │           41 ms rms
//   ×1,5  │ −52 ms │ 124 ms rms │ 591 ms max │ 11 / 30 s   │           96 ms rms
//   ×2    │ −42 ms │ 156 ms rms │ 646 ms max │ 27 / 22 s   │          139 ms rms
//   ×2    │ −313 ms│ 389 ms rms │ 791 ms max │ 54 / 22 s   │          165 ms rms
//
// Read the last column with the rest: what gives way first is not the decoder
// but the *delivery*. Every pass tracks the jitter of its own arrivals, and the
// resyncs appear exactly where that jitter approaches the 250 ms threshold — at
// ×2 the replay pushes 200 packets a second and `frame.t` arrives with a p95 of
// 322 ms, so the threshold is crossed by the scheduling, each crossing costs a
// decode restart, and the picture ends up short of the speed asked for. ×1,5 was
// clean twice and jolted once, on a busier minute; ×2 was never clean.
//
// The map said "past ~×2". The measurement puts the last speed worth following
// at ×1,5, and this is the one line to move if a quieter machine says otherwise.
const MAX_FOLLOW_SPEED = 1.5;

// Measuring the constant between the two domains. Not 0 for the probe: a request
// for 0 on a video already at 0 seeks nothing, so nothing is presented and the
// measurement would time out on every file.
//
// The first presentations after that seek are **skipped**, and that is the whole
// correction this measurement went through. Measured on the reference rush: a
// seek to 1.000 s displays PTS 0.9326 — two frames early — and the same file
// shows `mediaTime ≈ currentTime` to within 5 ms while it plays. The gap is the
// seek's landing, not the file's timeline (`/align/`'s stepping measures the
// same thing from the other side: a request at 5.000 s displays 4.929633).
// Taking that landing for the constant is not a small error: applied, it moved
// the picture 65 ms off — measured, over a full pass — which is precisely the
// silent shift the conversion exists to prevent.
const PROBE_CT_S    = 1.0;
const PROBE_SKIP    = 4;      // presentations belonging to the seek's landing
const PROBE_FRAMES  = 8;      // readings kept, median taken
const PROBE_WAIT_MS = 3000;

// The three states `layout.js` has to recognise by name, exported so that the
// producer and the reader cannot drift apart on a string. Every other state is
// prose the scene merely repeats, and needs no constant: what these three carry
// is a *decision*, and it is the same one in each case — **the picture on screen
// is the right one**. Following says so; a sweep says so while answering a hand
// rather than the take's timeline; a pause poses the instant's own frame and
// holds it. Anything else means what is displayed is not this instant's picture,
// which is exactly what the scene has to say out loud.
export const STATE_FOLLOWING = "suit le replay";
export const STATE_SCRUBBING = "balayage";
export const STATE_PAUSED    = "en pause";

const nowS = () => performance.now() / 1000;

export class VideoSyncClock {
  /** @param {HTMLVideoElement} video */
  constructor(video) {
    this.video = video;

    // The alignment: both anchors or neither (ADR 0001). Both live in the
    // `media` domain — `/align/` writes the PTS of the frame it displayed.
    this.onsetImuS   = null;
    this.onsetVideoS = null;

    // media − ct, measured once per file. 0 is not "unknown": it is the honest
    // value where nothing can be measured (no rVFC, hidden document), where the
    // two domains collapse into one by force because there is only one reading.
    this.offset         = 0;
    this.offsetMeasured = false;

    // What the 4 Hz snapshot says about the replay. Used for state, speed and
    // pause — never for time.
    this.active = false;
    this.paused = false;
    this.speed  = 1;

    this.media        = null;   // PTS of the displayed frame  (media domain)
    this.lastFrameT   = null;   // last frame.t received (s)
    this.lastTargetCt = null;   // the request that matches it (ct domain)
    this.driftAvg     = null;   // the drift both decisions are actually taken on
    this._lastFrameAt = null;   // wall clock of the last frame, for the smoothing
    this._detachedAt  = null;   // the speed the detached label was built for
    this.needHardSync = true;   // armed at boot, on reset, on resume
    this._lastSeekAt  = -Infinity;   // wall clock: this is a cost budget
    this._playPending = false;

    // A hand on the cursor, which outranks the replay while it lasts: sweeping
    // is reading a take, not playing it (ADR 0004). The pending position is the
    // last one asked for and not yet written.
    this.scrubbing    = false;
    this._scrubCt     = null;
    this._probe       = null;   // set while the domain offset is being measured
    this._abortProbe  = null;   // …and how to give that measurement up

    this.stats = {
      drift:      null,   // currentTime − target, ct domain: the command signal
      driftAvg:   null,   // the same, smoothed: what the two decisions read
      driftMedia: null,   // mediaTime − target, media domain: the drift really observed
      resyncs:    0,
      lastResync: null,   // {t, drift, cause}
      trim:       0,
      rateAsked:  1,
      rateGot:    1,
      state:      "inactif",
      outOfRange: false,
      detached:   false,
    };
  }

  // ── Settings ───────────────────────────────────────────────────────────────

  /** Both anchors together: an alignment is indivisible. */
  setAlignment(onsetImuS, onsetVideoS) {
    this.onsetImuS    = onsetImuS;
    this.onsetVideoS  = onsetVideoS;
    this.needHardSync = true;
  }

  get aligned() {
    return Number.isFinite(this.onsetImuS) && Number.isFinite(this.onsetVideoS);
  }

  /** True where the browser silently refused the rate we asked for. */
  get rateRefused() {
    return Math.abs(this.stats.rateAsked - this.stats.rateGot) > 1e-3;
  }

  /**
   * A new file is opening: everything measured about the previous one is void.
   *
   * The offset is a property of the file, so it does not survive a change of
   * file — and it is deliberately not remembered across one either.
   */
  newFile() {
    // A measurement still in flight belongs to the file that is going away. The
    // rVFC chain is permanent and survives the `src` swap, so left alone it would
    // go on collecting readings from the *new* file and write a constant made of
    // both.
    if (this._abortProbe) this._abortProbe();
    this.offset         = 0;
    this.offsetMeasured = false;
    this.media          = null;
    this.lastTargetCt   = null;
    this.needHardSync   = true;
    // A position asked for by the cursor names an instant of the file that is
    // going away; written into the new one it would be somewhere else entirely.
    this._scrubCt       = null;
    this._forget();
  }

  /**
   * Measure the constant between the two domains — in the regime that uses it.
   *
   * A seek, then a short muted run, then the median of `mediaTime` minus the
   * position the element reports while it hands that frame over. Both readings
   * are taken inside the same presentation callback, which is the only moment
   * the two domains name the same instant.
   *
   * Why not the single reading after the seek: on the reference rushes that
   * lands two frames early and is a fact about *seeking*, not about the file's
   * timeline (see the constants above). What this has to catch is an MP4 edit
   * list — a constant that survives playback — and that one shows up identically
   * in both regimes, so measuring in the playing one costs nothing and is not
   * fooled.
   *
   * The clock keeps its hands off the element while this runs (`_probe`), or the
   * 4 Hz snapshot's own `pause()` would stop the very playback being measured.
   *
   * Where nothing is ever presented — no `requestVideoFrameCallback`, or a
   * hidden document, which presents nothing either — it resolves false and the
   * offset stays 0. The caller is expected to ask again rather than to treat a
   * missing measurement as a measurement of zero.
   *
   * It borrows three things from the element and gives all three back: the
   * rate, the paused state, and **the position**. The third was missing, and it
   * mattered the moment a sweep could ask for a measurement: letting go of the
   * cursor left the picture at the probe's own second, seconds away from where
   * the hand had stopped, which looks exactly like a picture that is simply
   * wrong. Nothing else notices — the playing path hard-syncs on its next frame
   * whatever it finds.
   */
  measureOffset() {
    const v = this.video;
    if (!v.duration) return Promise.resolve(false);
    const probe = Math.min(PROBE_CT_S, v.duration / 2);
    return new Promise((resolve) => {
      const seen = [];
      const wasPaused = v.paused;
      const rate = v.playbackRate;
      const at   = v.currentTime;
      let n = 0, done = false;

      const finish = (ok) => {
        if (done) return;
        done = true;
        this._probe = this._abortProbe = null;
        v.playbackRate = rate;
        if (wasPaused) v.pause();
        // One seek, and only where it changes something: this runs on a file
        // that has just opened as often as after a sweep, and there the
        // position it is restoring is the zero it started from.
        if (Math.abs(v.currentTime - at) > 1e-3) v.currentTime = at;
        resolve(ok);
      };
      this._abortProbe = () => finish(false);

      this._probe = (media) => {
        if (++n <= PROBE_SKIP) return;
        seen.push(media - v.currentTime);
        if (seen.length < PROBE_FRAMES) return;
        // The median, not the mean: a single frame presented late would drag an
        // average, and what is being looked for is a constant, not a trend.
        const sorted = [...seen].sort((a, b) => a - b);
        this.offset         = sorted[sorted.length >> 1];
        this.offsetMeasured = true;
        this.needHardSync   = true;
        finish(true);
      };

      v.playbackRate = 1;
      v.currentTime  = probe;
      v.play().catch(() => finish(false));
      setTimeout(() => finish(false), PROBE_WAIT_MS);
    });
  }

  // ── The three inputs ───────────────────────────────────────────────────────

  /**
   * The reset `meta`: the model's timeline restarts (a pass beginning, a loop
   * turning over, a jump landing). The video has to move with it instead of
   * waiting for the drift to cross a threshold — the one moment where a hard
   * seek is the correct behaviour rather than a fallback.
   */
  onReset() {
    this.needHardSync = true;
    this.lastFrameT   = null;
    this._forget();
  }

  /** The panel snapshot (4 Hz): state, speed, pause. Never the time. */
  onPlayback(p) {
    const wasActive = this.active;
    this.active = !!(p && p.active);
    this.speed  = (p && p.speed) || 1;

    const paused = !!(p && p.paused);
    const pauseChanged = paused !== this.paused;
    if (pauseChanged) {
      this.paused = paused;
      // Resuming restarts the video where the replay resumed, not where it had
      // stopped: the one resync a pause justifies.
      if (!paused) this.needHardSync = true;
    }

    // A measurement is playing the file on purpose; this handler runs four times
    // a second and would pause it back on the next tick.
    if (this._probe) { this.stats.state = "mesure du décalage"; return; }

    // A hand is on the cursor. The state above is kept up to date — it is what
    // the picture will have to rejoin — but nothing is written to the element:
    // at 4 Hz this would fight the drag four times a second.
    if (this.scrubbing) return;

    if (!this.active) {
      this._standDown("inactif");
      if (wasActive) this.needHardSync = true;
      return;
    }
    if (this.paused) {
      this.video.pause();
      this.stats.state = STATE_PAUSED;
      // Paused, no frame arrives: the picture is posed once, on entering the
      // pause. The snapshot comes back through here at 4 Hz, and replaying the
      // seek on every pass would count four resyncs a second of stillness.
      if (pauseChanged && this.lastTargetCt !== null) this._seek(this.lastTargetCt, "pause");
    }
  }

  /**
   * One frame from the model. ~100 Hz: the hot path, which must allocate
   * nothing and write into the `<video>` only when there is a reason to.
   *
   * @param {number} t  frame.t, seconds since the take's first sample
   */
  onFrame(t) {
    this.lastFrameT = t;
    const v = this.video;

    // The replay may well still be running under a hand on the cursor — a
    // packet in flight, a pause not yet acknowledged. The cursor wins until it
    // is let go, or the picture would be dragged back and forth between two
    // drivers writing the same `currentTime`.
    if (this.scrubbing) return;
    if (!this.active || this.paused) return;
    if (this._probe)     { this.stats.state = "mesure du décalage"; return; }
    if (!this.aligned)   { this.stats.state = "non aligné";        return; }
    if (!v.duration)     { this.stats.state = "vidéo non chargée"; return; }

    // Past the speed the decoder can hold, following is worse than not
    // following: the trim stays pinned to its ceiling and every seek is a jolt
    // that corrects nothing. Freeze, say so, and stop claiming a drift.
    if (this.speed > MAX_FOLLOW_SPEED) {
      // Only on entering, or on a speed change while detached: this runs at the
      // model's rate and the state string is the one thing here that allocates.
      if (!this.stats.detached || this._detachedAt !== this.speed) {
        this.stats.detached = true;
        this._detachedAt = this.speed;
        this._standDown(`décrochée (×${this.speed})`);
      }
      this.needHardSync = true;   // rejoin the moment the speed comes back down
      return;
    }
    this.stats.detached = false;

    // The alignment lives in the reading domain; the element only understands
    // the request domain. `_requestCt` is where the two meet — a conversion,
    // never a sum — and the reading-domain target is that same number back in
    // its own domain, which is what the observed drift is measured against.
    const targetCt    = this._requestCt(t);
    const targetMedia = targetCt + this.offset;
    this.lastTargetCt = targetCt;

    // Outside the file: a take can start before the camera rolled or end after
    // it stopped — 8.8 s of the reference take's 58.9 have no picture at all, so
    // this is an ordinary case, not an edge one. Falling silent is more honest
    // than showing the first frame as though it were the right one, and there is
    // no drift to report because there is nothing being followed.
    if (targetCt < 0 || targetCt > v.duration) {
      this.stats.outOfRange = true;
      this._standDown(targetCt < 0 ? "avant la vidéo" : "après la vidéo");
      return;
    }
    this.stats.outOfRange = false;

    // Both differences stay inside one domain. `currentTime` is the command
    // signal — it is what a seek writes — and `mediaTime` is the only reading
    // that says which picture is on screen, so it is the drift actually seen.
    const drift = v.currentTime - targetCt;
    this.stats.drift = drift;
    this.stats.driftMedia = this.media === null ? null : this.media - targetMedia;

    // Smoothed in **wall** time, like the debounce and for the same reason: what
    // is being waited out is the arrival jitter of the frames, which is a
    // real-time phenomenon and does not slow down with the replay.
    const now = nowS();
    const dt  = this._lastFrameAt === null ? 0 : Math.max(0, now - this._lastFrameAt);
    this._lastFrameAt = now;
    const alpha = dt > 0 ? 1 - Math.exp(-dt / DRIFT_TAU_S) : 1;
    this.driftAvg = this.driftAvg === null ? drift
                                           : this.driftAvg + alpha * (drift - this.driftAvg);
    this.stats.driftAvg = this.driftAvg;

    if (this.needHardSync) {
      this._seek(targetCt, "reset");
      this.needHardSync = false;
    } else if (Math.abs(this.driftAvg) > HARD_RESYNC_S &&
               now - this._lastSeekAt > SEEK_COOLDOWN_S) {
      this._seek(targetCt, "seuil");
    } else {
      // Steady state: instead of jumping, play a little faster or a little
      // slower until the gap closes. It never jolts, and it is what removes the
      // constant bias a seek leaves behind.
      const trim = Math.max(-TRIM_MAX,
                            Math.min(TRIM_MAX, -this.driftAvg / TRIM_TAU_S));
      this.stats.trim = trim;
      this._setRate(this.speed * (1 + trim));
    }

    this.stats.state = STATE_FOLLOWING;
    // `v.paused` is read back rather than tracked with a flag of our own: the
    // browser pauses a muted video in a hidden tab, and the video has to restart
    // by itself when the tab comes back.
    if (v.paused && !this._playPending) {
      this._playPending = true;
      // `play()` is rejected every time the browser takes over (hidden tab,
      // power saving): without this catch each resume leaves an unhandled
      // rejection in the console and drowns the real errors. The rejection is
      // not an anomaly, it is the ordinary case.
      v.play().catch(() => {}).finally(() => { this._playPending = false; });
    }
  }

  /**
   * A picture has just been presented. `mediaTime` carries that frame's real
   * PTS — `currentTime` read back does not say which frame is on screen (the
   * spec has the official position set *before* the seek completes), so this is
   * where both the domain offset and the true drift are read.
   */
  onPresentedFrame(mediaTime) {
    this.media = mediaTime;
    if (this._probe) this._probe(mediaTime);
  }

  // ── The cursor ─────────────────────────────────────────────────────────────

  /**
   * Pose the picture at a take instant, asked for by a hand.
   *
   * Sweeping is reading a take, not playing it: the poses come off the track
   * beside the take and nothing reaches the bus (ADR 0004), so no `frame.t`
   * arrives to follow — this is the input that replaces it. While it lasts the
   * cursor is the only driver of the element, whatever the replay is doing.
   *
   * @param {number} tS  the cursor, in take seconds — the same timeline `frame.t`
   *                     counts on, so the same alignment converts it.
   * @returns {boolean}  whether a picture exists at that instant at all
   */
  scrub(tS) {
    const v = this.video;
    this.scrubbing = true;

    // A measurement is a short muted run of the file, which the pause below is
    // about to end. Giving it up beats letting it time out on readings taken
    // while the hand drags the picture around; `video.js` asks again once the
    // cursor is let go.
    if (this._abortProbe) this._abortProbe();

    // Searching is not playing. The picture is posed, one frame at a time.
    if (!v.paused) v.pause();
    this.stats.drift = this.stats.driftMedia = null;
    this.stats.trim  = 0;
    this._forget();
    // Whatever the picture ends up on, the replay will resume somewhere else
    // entirely: it rejoins with a seek rather than by waiting out a threshold.
    this.needHardSync = true;

    if (!this.aligned) { this.stats.state = "non aligné";        return false; }
    if (!v.duration)   { this.stats.state = "vidéo non chargée"; return false; }

    const targetCt = this._requestCt(tS);
    this.lastTargetCt = targetCt;

    // Out of the file, which is ordinary rather than exceptional — a take can
    // start before the camera rolled. Writing a bounded position would show a
    // frame from elsewhere as though it were this instant's.
    if (targetCt < 0 || targetCt > v.duration) {
      this.stats.outOfRange = true;
      this.stats.state = targetCt < 0 ? "avant la vidéo" : "après la vidéo";
      this._scrubCt = null;
      return false;
    }
    this.stats.outOfRange = false;
    this.stats.state = STATE_SCRUBBING;

    this._scrubCt = targetCt;
    if (nowS() - this._lastSeekAt >= SCRUB_COOLDOWN_S) this._flushScrub();
    return true;
  }

  /**
   * The hand lets go.
   *
   * The last position asked for is honoured here whatever the debounce says:
   * the cadence exists to spare the decoder *during* the drag, and stopping a
   * fraction of a second short of where the hand stopped would be a picture of
   * somewhere else.
   */
  endScrub() {
    this._flushScrub();
    this.scrubbing = false;
    this.needHardSync = true;
  }

  _flushScrub() {
    if (this._scrubCt === null) return;
    const ct = this._scrubCt;
    this._scrubCt = null;
    const v = this.video;
    v.currentTime = Math.max(0, Math.min(ct, v.duration || ct));
    this._lastSeekAt = nowS();
  }

  /**
   * A take instant as a *request* on the element.
   *
   * Both anchors, never their difference (ADR 0001), then the conversion out of
   * the reading domain the anchors live in. The one place the two domains meet,
   * so that the playing path and the cursor cannot drift apart.
   */
  _requestCt(tS) {
    return tS - this.onsetImuS + this.onsetVideoS - this.offset;
  }

  /** Forget the smoothed drift: there is nothing being followed to smooth. */
  _forget() {
    this.driftAvg = null;
    this._lastFrameAt = null;
    this.stats.driftAvg = null;
  }

  /**
   * Stop following, and say why.
   *
   * Three reasons reach this — no replay, no picture at this instant, too fast
   * to hold — and all three owe the same four things: the picture stops, the
   * state says which it is, no drift is reported (there is nothing being
   * followed, and leaving the last value would have it read as still measured),
   * and the smoothed drift is forgotten so the next frame back does not average
   * across the gap. Written once because forgetting one of the four in one
   * branch is exactly the defect that is invisible from the outside.
   */
  _standDown(state) {
    if (!this.video.paused) this.video.pause();
    this.stats.state = state;
    this.stats.drift = this.stats.driftMedia = null;
    this.stats.trim  = 0;
    this._forget();
  }

  // ── Writes into the <video> ────────────────────────────────────────────────

  _seek(targetCt, cause) {
    const v = this.video;
    v.currentTime = Math.max(0, Math.min(targetCt, v.duration || targetCt));
    this._lastSeekAt = nowS();
    this.driftAvg = 0;
    this.stats.driftAvg = 0;
    this.stats.resyncs++;
    this.stats.lastResync = { t: this.lastFrameT, drift: this.stats.drift, cause };
    this.stats.trim = 0;
    this._setRate(this.speed);
  }

  /**
   * `playbackRate = speed`, with no artificial limit — but the browser has one
   * of its own, and it applies it in silence. What it accepted is read back: a
   * refused rate would make the video diverge with nothing to say so. Measured:
   * taken as asked from ×0.25 to ×4.
   */
  _setRate(rate) {
    this.stats.rateAsked = rate;
    if (Math.abs(this.video.playbackRate - rate) > 1e-3) {
      try { this.video.playbackRate = rate; } catch { /* out of the browser's range */ }
    }
    this.stats.rateGot = this.video.playbackRate;
  }
}
