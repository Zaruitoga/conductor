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
// Requests live in `ct` (the `currentTime` domain), readings live in `media`
// (the `mediaTime` domain), and the two are never summed. Nothing guarantees
// they share an origin: an MP4 edit list, or a stream whose first frame is not
// at zero, installs a constant between them. Aiming at "`mediaTime` + an
// interval" then aims beside the mark, always on the same side — measured on the
// prototype's bench: with a −0.05 s offset, 29 seeks without the position moving
// at all. This page only ever seeks (a scrubber writes `ct`, the chain reads
// `media`), so the rule costs nothing here; it is stated because the frame
// stepping of #27 lands in this file and lives or dies by it.
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
// show, which is the ordinary case for a seek landing inside the current frame.
const SETTLE_MS     = 140;
const SETTLE_VSYNCS = 8;
const SEEKED_MS     = 400;   // a `seeked` that never comes must not hang the queue

export class VideoClock {
  constructor(video) {
    this.v      = video;
    this.media  = 0;      // PTS of the displayed frame   (mediaTime domain)
    this.ct     = 0;      // our request cursor            (currentTime domain)
    this.frames = 0;      // presentations since boot — an event counter, not a frame number
    this.rvfc   = RVFC ? null : false;   // null = undecided, false = never calls back
    this.dead   = false;
    this._fns     = [];
    this._want    = null;
    this._seeking = false;
    this._pump();
  }

  on(fn) { this._fns.push(fn); return this; }
  dispose() { this.dead = true; }
  _emit() { for (const f of this._fns) f(this); }

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

  // Ask for an instant, then wait for something to be presented. Deliberately
  // returns nothing: `media` is held by the chain and is the only authority on
  // where the video is.
  async _ask(t) {
    const v = this.v;
    const want = Math.max(0, v.duration ? Math.min(t, v.duration - 1e-3) : t);
    if (this.rvfc === false) {
      if (Math.abs(v.currentTime - want) > 1e-6) { v.currentTime = want; await this._seeked(); }
      this.ct = this.media = v.currentTime;
      return;
    }
    const seen = this.frames;
    if (Math.abs(v.currentTime - want) > 1e-6) { v.currentTime = want; await this._seeked(); }
    await this._settle(() => this.frames !== seen);
    this.ct = want;
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
}
