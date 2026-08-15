// video.js — where the video actually is, and how to ask it to move.
//
// One source of position for the whole page: `media`, the PTS of the frame the
// browser is *displaying*, reported by `requestVideoFrameCallback`. Reading
// `currentTime` instead would be reading our own request back — the HTML spec
// has the official position set *before* a seek completes, so it does not say
// which frame is on screen. Mixing the two sources is not a style question: the
// prototype (#9) read `currentTime` while playing and `media` while paused, and
// `media` — never refreshed during playback — dragged the scrubber and the curve
// cursor back to the last point clicked while the picture kept moving.
//
// Two domains, never added
// ------------------------
// Requests are made in the `currentTime` domain (`ct`, written by `seek()` and
// the probes below), readings come from the `mediaTime` domain (`media`), and
// the two are never summed. Nothing guarantees they share an origin: an MP4 edit
// list, or a stream whose first frame is not at zero, installs a constant
// between them. Aiming at "`mediaTime` + an interval" then aims beside the mark,
// always on the same side — measured on the prototype's bench: with a −0.05 s
// offset, 29 seeks without the position moving at all.
//
// So a step never computes where the next frame *is*. It walks away from the
// last request by small increments until the reported PTS **changes**, and it is
// that change — never the value reached — that answers. Whatever constant sits
// between the domains, the first frame reached that way is the neighbour.
//
// One permanent chain
// -------------------
// The `requestVideoFrameCallback` chain is armed once and re-arms itself
// forever. A callback armed *per seek* and abandoned after a timeout arrives
// late, and it is the *next* seek that collects it — reading the frame before.
// Here no callback belongs to any particular seek: there is only a state.

const RVFC = "requestVideoFrameCallback" in HTMLVideoElement.prototype;

// How long a presentation is still worth waiting for after `seeked`. The window
// opens once the browser says it has finished seeking, so a frame that is not
// presented within a few vsyncs is not late — there is simply no new frame to
// show, which is the ordinary case for a probe landing inside the current frame,
// i.e. the common case of a ramp. Beyond that, a presentation is *indiscernible
// from an absence*: rVFC carries no token tying a callback to the seek that
// caused it, so the only defence against taking a late one for an answer is the
// direction check in `_one`.
const SETTLE_MS     = 140;
const SETTLE_VSYNCS = 8;
const SEEKED_MS     = 400;   // a `seeked` that never comes must not hang the queue

// Measuring the file's cadence: how far the request is pushed on each try, and
// how many intervals are collected. 8 ms is a third of a 30 fps frame — small
// enough that a probe lands inside the current frame more often than not, which
// is what makes the *change* meaningful.
const PROBE_S        = 0.008;
const MEASURE_FRAMES = 5;

const MAX_PROBES  = 20;   // one step gives up after this many, rather than never
const MAX_PENDING = 4;    // key-repeat backlog: leaning on the key is not a queue
const FALLBACK_FPS = 30;  // only ever used where there is no rVFC to measure with

export class VideoClock {
  constructor(video) {
    this.v      = video;
    this.media  = 0;      // PTS of the displayed frame   (mediaTime domain)
    this.ct     = 0;      // our request cursor           (currentTime domain)
    this.frames = 0;      // presentations since boot — an event counter, not a frame number
    this.rvfc   = RVFC ? null : false;   // null = undecided, false = never calls back
    this.dead   = false;

    // The file's cadence, **measured** — and it only ever sizes a step. No
    // position is ever computed from it (a frame's identity is its PTS, and a
    // variable-cadence file has no "1/fps" at all). Read from the container it
    // could not be: `ffprobe` is not installed here and opening a binary
    // dependency for a step size was ruled out.
    this.dt     = 1 / FALLBACK_FPS;
    this.spread = null;   // [min, max] of the observed PTS intervals
    this.gran   = null;   // the shortest of them: the probe unit
    this.trace  = [];     // [asked, read, presentations] of the last probes

    this._ctStep  = null; // one frame's distance, measured in the *request* domain
    this._fns     = [];
    this._want    = null;
    this._seeking = false;
    this._pending = 0;
    this._busy    = false;
    this._pump();
  }

  on(fn) { this._fns.push(fn); return this; }
  dispose() { this.dead = true; }
  _emit() { for (const f of this._fns) f(this); }

  /** Frames per second — a number to show and to size a probe, never to place with. */
  get fps() { return this.dt > 0 ? 1 / this.dt : 0; }

  /**
   * Whether `dt` came from this file or from the fallback.
   *
   * `spread` is only ever set by a measurement that produced intervals, so it is
   * the honest witness. Saying "measured" off `rvfc` instead reads as a
   * measurement whenever the fallback happens to be right — and the fallback is
   * 30 fps, which is what the reference rushes are.
   */
  get measured() { return this.spread !== null; }

  // Does this browser report presented frames at all? Chrome and Safari do; an
  // embedded webview may never call back, and the page must say so rather than
  // freeze its cursor at zero. Called once the file has data — before that,
  // silence proves nothing.
  async decide(ms = 800) {
    if (this.rvfc !== null) return this.rvfc;
    await new Promise((r) => setTimeout(r, ms));
    if (this.dead || this.rvfc !== null) return this.rvfc === true;
    this.rvfc = false;
    this._pump();          // the chain is dead — start the polling fallback
    this._emit();
    return false;
  }

  _pump() {
    if (this.dead) return;
    if (RVFC && this.rvfc !== false) {
      this.v.requestVideoFrameCallback((_n, meta) => {
        if (this.dead || this.rvfc === false) return;
        this.media = meta.mediaTime;
        this.rvfc  = true;
        this.frames++;
        this._emit();
        this._pump();
      });
      return;
    }
    // Fallback: `currentTime` is all there is, so the two domains collapse into
    // one by force. Polled rather than read on demand so that playback still
    // moves the cursor.
    if (!this.v.paused) { this.media = this.v.currentTime; this._emit(); }
    setTimeout(() => this._pump(), this.v.paused ? 150 : 40);
  }

  // Wait for a presentation, bounded. `requestAnimationFrame` does not fire in a
  // hidden document (nothing is presented there either), so the timeout — not
  // the vsync count — is the deadline that matters: without it, switching tabs
  // mid-seek would suspend the queue for good.
  _settle(ok) {
    return new Promise((res) => {
      let n = 0, done = false;
      const fin = () => { if (!done) { done = true; res(); } };
      const tick = () => {
        if (done) return;
        (ok() || ++n > SETTLE_VSYNCS) ? fin() : requestAnimationFrame(tick);
      };
      requestAnimationFrame(tick);
      setTimeout(fin, SETTLE_MS);
    });
  }

  _seeked() {
    return new Promise((res) => {
      let done = false;
      const fin = () => {
        if (done) return;
        done = true;
        this.v.removeEventListener("seeked", fin);
        res();
      };
      this.v.addEventListener("seeked", fin);
      setTimeout(fin, SEEKED_MS);
    });
  }

  // Ask for an instant, then wait for something to be presented. Returns the
  // request actually made (clamped to the file), never a position: `media` is
  // held by the chain and is the only authority on where the video is.
  async _ask(t) {
    const v = this.v;
    const want = Math.max(0, v.duration ? Math.min(t, v.duration - 1e-3) : t);
    if (this.rvfc === false) {
      if (Math.abs(v.currentTime - want) > 1e-6) { v.currentTime = want; await this._seeked(); }
      this.ct = want;
      this.media = v.currentTime;      // no second domain to keep apart: this is all there is
      return want;
    }
    const seen = this.frames;
    if (Math.abs(v.currentTime - want) > 1e-6) { v.currentTime = want; await this._seeked(); }
    await this._settle(() => this.frames !== seen);
    this.ct = want;
    if (this.trace.length > 39) this.trace.shift();
    this.trace.push([+want.toFixed(4), +this.media.toFixed(4), this.frames - seen]);
    return want;
  }

  // One pending request, latest wins. A drag emits dozens of these a second and
  // only the last one is worth landing; letting them queue made the last
  // resolved promise overwrite the position, so the cursor jumped back to a
  // point clicked several seeks ago.
  async seek(t) {
    this._want = t;
    if (this._seeking) return;
    this._seeking = true;
    while (this._want != null && !this.dead) {
      const target = this._want;
      this._want = null;
      await this._ask(target);
      this._emit();
    }
    this._seeking = false;
  }

  // ── The file's cadence, measured ──────────────────────────────────────────
  //
  // Measured in pause, by pushing the request forward in `PROBE_S` steps until
  // the reported PTS changes — the same closed loop a step uses, run a few
  // times. Going through `play()` would depend on autoplay, and reading
  // `getVideoPlaybackQuality()` would need the whole file played first.
  //
  // What comes out sizes the probe and gets displayed. It never places anything.
  async measure(n = MEASURE_FRAMES) {
    if (this.dead || this.rvfc === false) return;

    // First, obtain a *reading*. `media` starts at an initial value nothing
    // reported, and a request for 0 on a video already at 0 seeks nothing — so
    // nothing is presented. Measuring from there would fold the offset between
    // the two domains into the first interval, which is the one mistake this
    // file exists to avoid. `frames` counts presentations, so it is what says
    // whether `media` has ever been anything but a default.
    let asked = await this._ask(0), last = null;
    for (let i = 0; this.frames === 0 && i < MAX_PROBES; i++) {
      asked = await this._ask(asked + PROBE_S);
      if (asked === last) break;
      last = asked;
    }
    if (this.frames === 0) return;   // nothing is ever presented here: nothing to measure

    const dPts = [], dCt = [];
    let cur = this.media;
    for (let i = 0; i < n; i++) {
      const from = asked;
      last = null;
      for (let g = 0; Math.abs(this.media - cur) < 1e-9 && g < 40; g++) {
        asked = await this._ask(asked + PROBE_S);
        if (asked === last) break;              // clamped at the end of the file
        last = asked;
      }
      if (this.media <= cur) break;
      dPts.push(this.media - cur); dCt.push(asked - from); cur = this.media;
    }
    // A single aberrant interval would skew the probe unit the whole stepping
    // depends on: take the median and keep only what orbits it.
    const trim = (a) => {
      const s = [...a].sort((x, y) => x - y), med = s[s.length >> 1];
      return [med, s.filter((x) => x > med * 0.6 && x < med * 1.6)];
    };
    if (dPts.length) {
      const [med, keep] = trim(dPts);
      this.dt      = med;
      this.spread  = [keep[0] ?? med, keep[keep.length - 1] ?? med];
      this.gran    = this.spread[0];
      this._ctStep = trim(dCt)[0];
    }
    await this._ask(0);
    this._emit();
  }

  // ── One frame, exactly ────────────────────────────────────────────────────

  // `ct` sits just past a frame boundary (within one probe of it). Walk away
  // from it by δ until the reported PTS *changes*: the first frame reached that
  // way is necessarily the neighbour, whatever offset may sit between the two
  // domains. What is learnt on the way is one frame's distance measured *in the
  // request domain* (`_ctStep`), so the next step starts just under it — one or
  // two probes per keypress, and never the overshoot a wider start would risk.
  async _one(dir) {
    const base = this.ct, before = this.media;
    // The change must go the way we asked. The two domains can be offset, but
    // the correspondence stays increasing: a reading that moves backwards while
    // we step forward can only be a late callback.
    const changed = () => dir > 0 ? this.media > before + 1e-9 : this.media < before - 1e-9;
    if (this.rvfc === false) { await this._ask(base + dir * this.dt); return; }

    const d = (this.gran ?? this.dt) / 4;
    let eps = dir > 0 ? Math.max(d, (this._ctStep ?? 4 * d) - d) : d;
    let last = null;
    for (let i = 0; i < MAX_PROBES; i++) {
      const asked = await this._ask(base + dir * eps);
      if (changed()) { if (dir > 0) this._ctStep = eps; return; }
      if (asked === last) break;          // clamped at an end of the file: no frame that way
      last = asked;
      eps += d;
    }
    this.ct = base;                       // nothing moved: neither does the cursor
  }

  // Keypresses stack and drain one frame at a time — never merged into a jump,
  // or the precision is lost exactly where it is being looked for. The queue is
  // bounded: hammering the key because nothing seems to move must not leave
  // thirty steps to replay.
  async step(dir) {
    this._pending = Math.max(-MAX_PENDING,
                             Math.min(MAX_PENDING, this._pending + Math.sign(dir)));
    if (this._busy) return;
    this._busy = true;
    while (this._pending && !this.dead) {
      const d = Math.sign(this._pending);
      this._pending -= d;
      await this._one(d);
      this._emit();
    }
    this._busy = false;
  }

  // A wide jump: approximate by nature, and that is fine — it is for crossing
  // ground, and the frame reading tells the truth on arrival. Still one domain:
  // a request cursor plus a distance measured among requests.
  jump(n) { return this.seek(this.ct + n * (this._ctStep ?? this.dt)); }
}
