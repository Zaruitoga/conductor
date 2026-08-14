// curve.js — the take's raw |ω|, reduced at the draw and zoomed for free.
//
// ADR 0002: `GET …/onset` sends every sample, and reduction is the client's job.
// It is min/max per pixel column, never decimation — the front this page exists
// to look at is a few samples wide, and one sample in N smooths away exactly
// that. A column keeps the lowest and highest value that fell in it, so a
// one-sample spike survives at any zoom instead of being averaged into the
// neighbourhood.
//
// The whole curve lives in memory, so zooming is a redraw and never a round
// trip. That is the point of the ADR: checking an alignment *is* zooming, it is
// the gesture repeated most, and a server-side reduction would make the server
// choose the resolution the eye is asking for.

// Zooming in stops when the window is this many sample intervals wide — derived
// from the take's own cadence rather than picked, so a 400 Hz take zooms four
// times further than a 100 Hz one and both stop with a handful of samples on
// screen, which is where "at the sample" becomes literally visible.
const MIN_SPAN_SAMPLES = 8;
const MIN_SPAN_S       = 0.005;

// Below this many samples per pixel, each one is also drawn as a dot: at that
// zoom the polyline is what carries the shape, and the dots are what prove the
// trace is the samples themselves and not a resampled curve.
const DOTS_BELOW = 0.33;

const TICK_STEPS = [0.005, 0.01, 0.02, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60];

const COL = {
  bg:      "#11151a",
  grid:    "#232a31",
  axis:    "#5c666f",
  trace:   "#4a9eff",
  pick:    "#3fb950",
  other:   "#7d8892",
  playhead:"#f85149",
};

export class CurveView {
  /**
   * @param canvas   the <canvas> to own
   * @param onPick   called with a take-time when the user clicks the curve
   * @param onView   called whenever the zoom window changes
   */
  constructor(canvas, { onPick, onView } = {}) {
    this.cv      = canvas;
    this.onPick  = onPick ?? (() => {});
    this.onView  = onView ?? (() => {});
    this.samples = [];            // [[t, |ω|], …] — the whole take, unreduced
    this.dur     = 0;
    this.t0 = 0; this.t1 = 1;
    this.ymax    = 1;
    this._cache  = null;          // reduction, keyed by window + width
    this._render = null;          // last {candidates, selected, playhead}
    this._drag   = null;
    this._wire();
  }

  // ── Data ──────────────────────────────────────────────────────────────────

  setTake(samples, durationS) {
    this.samples = samples ?? [];
    this.dur     = Math.max(durationS || 0, 1e-3);
    this.ymax    = 1;
    this._cache  = null;
    this.setView(0, this.dur);
  }

  get minSpan() {
    const n = this.samples.length;
    const step = n > 1 ? (this.samples[n - 1][0] - this.samples[0][0]) / (n - 1) : 0;
    return Math.max(MIN_SPAN_SAMPLES * step, MIN_SPAN_S);
  }

  setView(t0, t1) {
    const span = Math.min(Math.max(t1 - t0, this.minSpan), this.dur);
    let a = t0, b = a + span;
    if (a < 0)         { a = 0;              b = span; }
    if (b > this.dur)  { b = this.dur;       a = b - span; }
    if (a !== this.t0 || b !== this.t1) {
      this.t0 = Math.max(0, a); this.t1 = b;
      this._cache = null;
      this.onView(this.t0, this.t1);
    }
  }

  get zoomed() { return this.t1 - this.t0 < this.dur - 1e-9; }

  resetView() { this.setView(0, this.dur); this.draw(); }

  /** Bring an instant into view, keeping the current zoom. */
  reveal(t) {
    if (t == null || (t >= this.t0 && t <= this.t1)) return;
    const span = this.t1 - this.t0;
    this.setView(t - span / 2, t + span / 2);
  }

  // ── Gestures (client-side zoom: no round trip, ever) ──────────────────────

  _wire() {
    this.cv.addEventListener("wheel", (e) => {
      if (!this.samples.length) return;
      e.preventDefault();
      const span = this.t1 - this.t0;
      const at   = this.t0 + (this._x(e) / this._w()) * span;   // zoom about the pointer
      const k    = Math.exp((e.deltaY > 0 ? 1 : -1) * 0.2);      // up = in, down = out
      const next = Math.min(Math.max(span * k, this.minSpan), this.dur);
      const f    = span > 0 ? (at - this.t0) / span : 0.5;
      this.setView(at - f * next, at - f * next + next);
      this.draw();
    }, { passive: false });

    this.cv.addEventListener("pointerdown", (e) => {
      this.cv.setPointerCapture(e.pointerId);
      this._drag = { x: e.clientX, t0: this.t0, moved: false };
    });
    this.cv.addEventListener("pointermove", (e) => {
      if (!this._drag) return;
      const dx = e.clientX - this._drag.x;
      if (Math.abs(dx) > 2) this._drag.moved = true;
      if (!this._drag.moved) return;
      const span = this.t1 - this.t0;
      const shift = (dx / this._w()) * span;
      this.setView(this._drag.t0 - shift, this._drag.t0 - shift + span);
      this.draw();
    });
    this.cv.addEventListener("pointerup", (e) => {
      const drag = this._drag;
      this._drag = null;
      if (!drag || drag.moved || !this.samples.length) return;
      this.onPick(this.t0 + (this._x(e) / this._w()) * (this.t1 - this.t0));
    });
    this.cv.addEventListener("pointercancel", () => { this._drag = null; });
    this.cv.addEventListener("dblclick", () => this.resetView());
  }

  _w() { return this.cv.clientWidth || 1; }
  _x(e) { return e.clientX - this.cv.getBoundingClientRect().left; }

  // ── Reduction ─────────────────────────────────────────────────────────────

  // min/max per pixel column. The four values per column are what makes the
  // trace both honest and continuous: min/max hold the extremes that a
  // decimation would lose, first/last join the column to its neighbours so the
  // line reads as one curve rather than a picket fence.
  _reduce(w) {
    const key = `${this.t0}|${this.t1}|${w}`;
    if (this._cache && this._cache.key === key) return this._cache;

    const min = new Float64Array(w).fill(Infinity);
    const max = new Float64Array(w).fill(-Infinity);
    const first = new Float64Array(w);
    const last = new Float64Array(w);
    const hit = new Uint8Array(w);

    const span = Math.max(this.t1 - this.t0, 1e-9);
    let n = 0, top = 0, past = false;
    // The samples are sorted, so the window is one contiguous run: find its
    // start by bisection rather than scanning a 15-minute take from row 0 on
    // every redraw. It starts one sample early and ends one sample late, so the
    // trace enters and leaves the panel instead of stopping short of both
    // edges — but those two contribute to the columns only, never to the
    // vertical scale: a spike sitting one pixel off screen must not rescale the
    // window it is not in.
    for (let i = this._firstAtOrAfter(this.t0); i < this.samples.length; i++) {
      const [t, v] = this.samples[i];
      if (past) break;
      if (t > this.t1) past = true;
      n++;
      let c = Math.floor(((t - this.t0) / span) * w);
      if (c < 0) c = 0; else if (c >= w) c = w - 1;
      if (!hit[c]) { hit[c] = 1; first[c] = v; }
      last[c] = v;
      if (v < min[c]) min[c] = v;
      if (v > max[c]) max[c] = v;
      if (v > top && t >= this.t0 && !past) top = v;
    }
    // The vertical scale is the window's, not the take's — and the axis is
    // labelled with it, in the same pass, so the two can never disagree. A
    // scale fixed to the whole take was tried first and squashes the thing this
    // page exists to look at: the front is 0.5–1 rad/s against a take that
    // peaks near 15, so zooming into it showed a flat line. What made the fixed
    // scale tempting — a quiet stretch blown up to full height reads as
    // movement — is answered by the label rather than by hiding the shape, and
    // it cannot mislead the actual decision anyway: the candidates are found
    // server-side against a fixed threshold, so nobody is eyeballing a level.
    this.ymax = top > 0 ? top * 1.15 : 1;
    return (this._cache = { key, min, max, first, last, hit, count: n, w, ymax: this.ymax });
  }

  _firstAtOrAfter(t) {
    let lo = 0, hi = this.samples.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (this.samples[mid][0] < t) lo = mid + 1; else hi = mid;
    }
    return Math.max(0, lo - 1);          // one back, so the line enters from off-screen
  }

  // ── Drawing ───────────────────────────────────────────────────────────────

  draw(state) {
    if (state) this._render = state;
    const { candidates = [], selected = -1, playhead = null } = this._render ?? {};

    const dpr = Math.min(devicePixelRatio || 1, 2);
    const w = this._w(), h = this.cv.clientHeight || 1;
    if (this.cv.width !== Math.round(w * dpr) || this.cv.height !== Math.round(h * dpr)) {
      this.cv.width = Math.round(w * dpr);
      this.cv.height = Math.round(h * dpr);
      this._cache = null;
    }
    const g = this.cv.getContext("2d");
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.fillStyle = COL.bg;
    g.fillRect(0, 0, w, h);

    const span = Math.max(this.t1 - this.t0, 1e-9);
    const X = (t) => ((t - this.t0) / span) * w;

    this._grid(g, w, h, span, X);
    if (!this.samples.length) return;      // no trace, hence no scale to name

    // The reduction fixes the vertical scale, so it runs before anything is
    // placed against it.
    const red = this._reduce(Math.max(1, Math.round(w)));
    const Y = (v) => h - (v / red.ymax) * (h - 14) - 12;

    g.strokeStyle = COL.trace;
    g.lineWidth = 1.25;
    g.beginPath();
    let started = false;
    for (let c = 0; c < red.w; c++) {
      if (!red.hit[c]) continue;
      const x = c + 0.5;
      if (!started) { g.moveTo(x, Y(red.first[c])); started = true; }
      else g.lineTo(x, Y(red.first[c]));
      if (red.max[c] !== red.min[c]) { g.lineTo(x, Y(red.min[c])); g.lineTo(x, Y(red.max[c])); }
      g.lineTo(x, Y(red.last[c]));
    }
    g.stroke();

    // Zoomed past one sample per pixel: show the samples themselves.
    if (red.count > 0 && red.count / red.w < DOTS_BELOW) {
      g.fillStyle = COL.trace;
      for (let i = this._firstAtOrAfter(this.t0); i < this.samples.length; i++) {
        const [t, v] = this.samples[i];
        if (t > this.t1) break;
        g.beginPath(); g.arc(X(t), Y(v), 2, 0, 6.284); g.fill();
      }
    }

    candidates.forEach((c, i) => {
      const on = i === selected;
      this._marker(g, X(c.t_s), h, on ? COL.pick : COL.other, on ? 2 : 1, !on);
      if (X(c.t_s) >= -40 && X(c.t_s) <= w + 40) {
        g.fillStyle = on ? COL.pick : COL.other;
        g.font = on ? "600 10px system-ui" : "10px system-ui";
        g.fillText(`${i + 1}`, Math.min(X(c.t_s) + 4, w - 10), 10);
      }
    });

    if (playhead != null && playhead >= this.t0 && playhead <= this.t1) {
      this._marker(g, X(playhead), h, COL.playhead, 1.5, false);
    }

    this._axis(g);
  }

  _marker(g, x, h, color, width, dashed) {
    g.strokeStyle = color;
    g.lineWidth = width;
    g.setLineDash(dashed ? [3, 3] : []);
    g.beginPath(); g.moveTo(x, 0); g.lineTo(x, h); g.stroke();
    g.setLineDash([]);
  }

  _grid(g, w, h, span, X) {
    const step = TICK_STEPS.find((s) => span / s < Math.max(3, w / 90)) ?? 120;
    const digits = step < 0.1 ? 3 : step < 1 ? 2 : 1;
    g.strokeStyle = COL.grid;
    g.fillStyle = COL.axis;
    g.font = "10px system-ui";
    g.lineWidth = 1;
    for (let t = Math.ceil(this.t0 / step) * step; t <= this.t1; t += step) {
      const x = Math.round(X(t)) + 0.5;
      g.beginPath(); g.moveTo(x, 0); g.lineTo(x, h); g.stroke();
      g.fillText(`${t.toFixed(digits)} s`, x + 3, h - 3);
    }
  }

  // The top of the scale, named. Without it an autoscaled window would be a
  // shape with no magnitude, and a quiet stretch blown up to full height would
  // be indistinguishable from a violent one.
  _axis(g) {
    g.fillStyle = COL.axis;
    g.font = "10px system-ui";
    const y = this.ymax;
    g.fillText(`${y < 1 ? y.toFixed(2) : y.toFixed(1)} rad/s`, 4, 10);
  }
}
