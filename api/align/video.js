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
// A measurement probes in `PROBE_S` (8 ms) rather than in quarter-frames, so it
// needs more room to cross one frame — and it is the one walk allowed to be slow.
const MEASURE_PROBES = 40;
const MAX_PENDING = 4;    // key-repeat backlog: leaning on the key is not a queue
const FALLBACK_FPS = 30;  // only ever used where there is no rVFC to measure with

// Coming to a standstill: how long without an *unrequested* presentation counts
// as "the pipeline has drained", and the wall-clock ceiling on waiting for it.
// There is no event for this — `pause` says the element has stopped advancing,
// not that the compositor has stopped handing over what it already holds.
const STILL_QUIET_MS = 40;    // ~2.5 vsyncs at 60 Hz
const STILL_MS       = 250;

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
    this.origin = null;   // PTS of the file's first frame — the frame numbers' zero

    // What the last gesture asked for, in frames, and what it got. Two numbers
    // rather than one because their *disagreement* is the only thing that says a
    // step went wrong: a walk that overshoots lands on a perfectly plausible
    // frame, and nothing else on the page would ever mention it.
    this.last   = null;   // {want, got} | null

    // Two distances in the request domain, and they must not be one field.
    //
    //   `_ctFrame` — one frame, measured once by `measure()`. It is what a jump
    //     multiplies, so it has to stay put.
    //   `_ctStep`  — where a forward probe *starts*. It adapts, and it is
    //     allowed to shrink: starting short only costs an extra probe, while
    //     starting long risks stepping over a frame.
    //
    // They were one field. A backward step leaves the cursor a hair inside the
    // previous frame, so the next forward step succeeds on its first probe and
    // writes that shorter distance back — and walking back and forth is exactly
    // what comparing against the pinned frame *is*. Four round trips took `⇧`
    // from ten frames to five, silently. The bench pins it now.
    this._ctFrame = null;
    this._ctStep  = null;
    this._wrote   = null; // the last value actually written to the element
    // When a presentation last arrived that *no probe had asked for* — playback,
    // or a frame still in the compositor after one. It is what says the picture
    // is still moving on its own; presentations we caused are not evidence of
    // that, which is why the two are told apart rather than counted together.
    this._asking  = false;
    this._freeAt  = 0;
    this._fns     = [];
    this._want    = null;
    this._seeking = false;
    this._pending = 0;
    this._jumping = 0;
    this._busy    = false;
    this._gen     = 0;    // bumped by a user gesture: a measurement in flight gives up
    this._queue   = Promise.resolve();
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

  /**
   * The number of the displayed frame, counted from the file's first — or null.
   *
   * **Derived, and a label only.** It is the PTS divided by the measured cadence,
   * and nothing in this class ever navigates by it: a frame's identity is still
   * its PTS, which is what every anchor is written from. It exists because a PTS
   * cannot be verified by eye — "0.033 further" and "one frame further" are the
   * same claim, and only the second is checkable at a glance.
   *
   * Hence the two conditions. Without `origin` there is no zero to count from,
   * and without a *measured* cadence the divisor is the 30 fps fallback, which
   * would number a 25 fps file confidently and wrongly. Both absent, the page
   * shows the PTS alone rather than a number it cannot stand behind.
   */
  get frameNo() {
    if (this.origin === null || !this.measured) return null;
    return Math.round((this.media - this.origin) / this.dt);
  }

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
        if (!this._asking) this._freeAt = performance.now();
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
      this.ct = this._wrote = want;
      this.media = v.currentTime;      // no second domain to keep apart: this is all there is
      return want;
    }
    const seen = this.frames;
    // Where the element already sits, nothing is asked and nothing new will be
    // shown: waiting out the settle budget would only widen the window in which
    // some *earlier* probe's late presentation can land and be taken for this
    // one's answer.
    const moved = Math.abs(v.currentTime - want) > 1e-6;
    // Held across the wait, not just the write: what marks a presentation as
    // ours is that a probe was outstanding when it arrived. Anything landing
    // outside that is either playback or a frame the compositor still held, and
    // `_still()` is what waits those out.
    this._asking = true;
    try {
      if (moved) {
        v.currentTime = want;
        await this._seeked();
        await this._settle(() => this.frames !== seen);
      }
    } finally {
      this._asking = false;
    }
    this.ct = this._wrote = want;
    return want;
  }

  /**
   * Bring the element to a standstill, and adopt where it actually is.
   *
   * Two things a step cannot begin without, and the same omission produced both.
   *
   * A playing element presents a frame every vsync, so the "has the reading
   * changed?" test that a walk turns on is trivially true before the first probe
   * has done anything — the step reports success and lands wherever playback had
   * got to. `pause()` is not enough on its own: it says the element has stopped
   * advancing, not that the compositor has stopped handing over what it already
   * holds, and those in-flight frames land a vsync or two later. Keying the wait
   * off `paused` would therefore skip exactly the window it exists for — the
   * page pauses *before* it steps, since that is what entering detail mode does.
   * So the standstill is observed instead: nothing presented that no probe asked
   * for. On the common path — stepping in a video that has been still for a
   * keypress already — that is true on entry and costs nothing.
   *
   * And `ct` is the **request** cursor: only `_ask` writes it, so while the video
   * plays it still names the last seek, seconds behind the picture. `currentTime`
   * is the one legitimate read-back — it is the request domain itself, not the
   * reading domain — so adopting it is how the cursor rejoins a picture that
   * moved without us. Guarded by `_wrote` so that it only ever happens when
   * something *else* moved the element: a walk that found no frame deliberately
   * rolls `ct` back while the element stays where the last probe left it, and
   * that rollback must survive.
   */
  async _still() {
    const v = this.v;
    if (!v.paused) v.pause();
    const deadline = performance.now() + STILL_MS;
    while (performance.now() - this._freeAt < STILL_QUIET_MS
           && performance.now() < deadline && !this.dead) {
      // Bounded by the timeout as well as the vsync: a hidden document never
      // fires `requestAnimationFrame` at all, and must not hang the queue.
      await new Promise((r) => {
        const id = setTimeout(r, 24);
        requestAnimationFrame(() => { clearTimeout(id); r(); });
      });
    }
    if (this._wrote === null || Math.abs(v.currentTime - this._wrote) > 1e-6) {
      this.ct = this._wrote = v.currentTime;
    }
  }

  // Walk away from `from` by `d` at a time until `done()`, bounded.
  //
  // The one shape all three drivers share: a probe is a request, and what
  // answers is a *change* in the reading — so the loop can only stop on the
  // reading, never on having reached some computed value. Returns the last
  // request made. Stops early where the clamp stops moving it, which is the end
  // of the file and therefore no frame that way.
  async _walk(from, d, done, limit = MAX_PROBES) {
    let asked = from, last = null;
    if (done()) return asked;          // already true: probing would only move the cursor
    for (let i = 0; i < limit && !this.dead; i++) {
      asked = await this._ask(asked + d);
      if (done() || asked === last) return asked;
      last = asked;
    }
    return asked;
  }

  // One driver at a time on the element.
  //
  // `seek()`, `step()` and `measure()` all write `v.currentTime` and all await
  // the same `seeked`. Interleaved, each collects the other's event, and the
  // probe reading the result would be measuring the other's request — the same
  // confusion as the two domains, one level up. `measure()` runs a dozen probes
  // right after `loadeddata`, which is exactly when a scrubber drag is likeliest,
  // so this is not hypothetical.
  _drive(fn) {
    const run = this._queue.then(fn, fn);
    this._queue = run.catch(() => {});
    return run;
  }

  // One pending request, latest wins. A drag emits dozens of these a second and
  // only the last one is worth landing; letting them queue made the last
  // resolved promise overwrite the position, so the cursor jumped back to a
  // point clicked several seeks ago.
  //
  // `_gen` says a hand is on the controls, so a measurement still probing gives
  // up rather than making the user wait for it.
  seek(t) {
    this._want = t;
    this._gen++;
    this.last = null;          // a scrub travels no whole number of frames
    if (this._seeking) return this._queue;
    this._seeking = true;
    return this._drive(async () => {
      while (this._want != null && !this.dead) {
        const target = this._want;
        this._want = null;
        await this._ask(target);
        this._emit();
      }
      this._seeking = false;
    });
  }

  // ── The file's cadence, measured ──────────────────────────────────────────
  //
  // Measured in pause, by pushing the request forward in `PROBE_S` steps until
  // the reported PTS changes — the same closed loop a step uses, run a few
  // times. Going through `play()` would depend on autoplay, and reading
  // `getVideoPlaybackQuality()` would need the whole file played first.
  //
  // What comes out sizes the probe and gets displayed. It never places anything.
  //
  // It is abandoned the moment a user gesture arrives or the take changes: it
  // drives the *shared* `<video>` for a second or two, and left running past a
  // `dispose()` it would be dragging the **next** take's picture around, then
  // snapping it to zero on the way out.
  measure(n = MEASURE_FRAMES) {
    if (this.dead || this.rvfc === false) return Promise.resolve();
    const gen = ++this._gen;
    const mine = () => !this.dead && this._gen === gen;
    return this._drive(async () => {
      if (!mine()) return;

      // First, obtain a *reading*. `media` starts at an initial value nothing
      // reported, and a request for 0 on a video already at 0 seeks nothing — so
      // nothing is presented. Measuring from there would fold the offset between
      // the two domains into the first interval, which is the one mistake this
      // file exists to avoid. `frames` counts presentations, so it is what says
      // whether `media` has ever been anything but a default.
      let asked = await this._ask(0);
      asked = await this._walk(asked, PROBE_S, () => this.frames > 0 || !mine());
      if (this.frames === 0 || !mine()) return;  // nothing presented here: nothing to measure

      // The frame numbering's zero: the PTS of the frame at the head of the
      // file, read like everything else rather than assumed to be 0 — the same
      // offset that makes the two domains incomparable puts a first frame
      // wherever the container says. Taken twice (here, and again from the
      // return to 0 below) and kept at the lower of the two, since a first probe
      // that had to walk forward may already have crossed into frame 1.
      this.origin = this.media;

      const dPts = [], dCt = [];
      let cur = this.media;
      for (let i = 0; i < n && mine(); i++) {
        const from = asked;
        asked = await this._walk(asked, PROBE_S,
                                 () => Math.abs(this.media - cur) > 1e-9 || !mine(),
                                 MEASURE_PROBES);
        if (this.media <= cur) break;
        dPts.push(this.media - cur); dCt.push(asked - from); cur = this.media;
      }
      if (!mine()) return;
      // A single aberrant interval would skew the probe unit the whole stepping
      // depends on: take the median and keep only what orbits it.
      const trim = (a) => {
        const s = [...a].sort((x, y) => x - y), med = s[s.length >> 1];
        return [med, s.filter((x) => x > med * 0.6 && x < med * 1.6)];
      };
      if (dPts.length) {
        const [med, keep] = trim(dPts);
        this.dt       = med;
        this.spread   = [keep[0] ?? med, keep[keep.length - 1] ?? med];
        this.gran     = this.spread[0];
        this._ctFrame = trim(dCt)[0];    // the jump's unit: measured once, then left alone
      }
      if (mine()) {
        await this._ask(0);
        this.origin = Math.min(this.origin, this.media);
      }
      this._emit();
    });
  }

  // ── One frame, exactly ────────────────────────────────────────────────────

  // `ct` sits just past a frame boundary (within one probe of it). Walk away
  // from it by δ until the reported PTS *changes*: the first frame reached that
  // way is necessarily the neighbour, whatever offset may sit between the two
  // domains. What is learnt on the way is where the *next* forward probe should
  // start (`_ctStep`) — one or two probes per keypress, and never the overshoot
  // a wider start would risk. That hint is not the jump's unit: see the field.
  async _one(dir) {
    const base = this.ct, before = this.media;
    // The change must go the way we asked. The two domains can be offset, but
    // the correspondence stays increasing: a reading that moves backwards while
    // we step forward can only be a late callback.
    const changed = () => dir > 0 ? this.media > before + 1e-9 : this.media < before - 1e-9;
    if (this.rvfc === false) { await this._ask(base + dir * this.dt); return; }

    const d = (this.gran ?? this.dt) / 4;
    let eps = dir > 0 ? Math.max(d, (this._ctStep ?? this._ctFrame ?? 4 * d) - d) : d;
    let last = null;
    for (let i = 0; i < MAX_PROBES && !this.dead; i++) {
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
  step(dir) {
    this._pending = Math.max(-MAX_PENDING,
                             Math.min(MAX_PENDING, this._pending + Math.sign(dir)));
    return this._run();
  }

  // The one loop both gestures drain, rather than one each: they share the
  // element, so a jump arriving while a step is in flight would otherwise be
  // dropped on the floor — the second gesture sees `_busy` and returns, and
  // nothing is left to pick its backlog up. Jumps are taken first within a
  // batch; the two only ever mix under hammering, and the reading tells the
  // truth on arrival either way.
  _run() {
    this._gen++;
    if (this._busy) return this._queue;
    this._busy = true;
    return this._drive(async () => {
      await this._still();
      const from = this.frameNo;
      let want = 0;
      while ((this._pending || this._jumping) && !this.dead) {
        if (this._jumping) {
          const n = this._jumping;
          this._jumping = 0;
          want += n;
          await this._ask(this.ct + n * (this._ctFrame ?? this.dt));
        } else {
          const d = Math.sign(this._pending);
          this._pending -= d;
          want += d;
          await this._one(d);
        }
        this._emit();
      }
      this._report(want, from);
      this._busy = false;
    });
  }

  // What the gesture asked for against what the reading says it got. Kept apart
  // from the walk itself: a walk answers on a *change*, and cannot know how many
  // frames that change spanned — only the numbering can say, and only once the
  // cadence has been measured. Absent that, no claim is made at all.
  _report(want, from) {
    const got = this.frameNo;
    this.last = (from === null || got === null) ? null : { want, got: got - from };
    this._emit();
  }

  // A wide jump: approximate by nature, and that is fine — it is for crossing
  // ground, and the frame reading tells the truth on arrival. Still one domain:
  // a request cursor plus a distance measured among requests.
  //
  // It multiplies `_ctFrame`, the distance *measured once*, never the adaptive
  // probe hint — which shrinks with use and would quietly shorten every jump.
  //
  // Unlike a step, repeats *are* merged: a jump is coarse by construction, so
  // two of them are one twice as long, where merging two steps would lose the
  // precision they exist for. It stands still first for the same reason a step
  // does — `ct` is a request cursor, and a jump measured from a stale one lands
  // ten frames away from where the picture was, not from where it is.
  jump(n) {
    this._jumping += n;
    return this._run();
  }
}
