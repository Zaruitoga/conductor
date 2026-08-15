// _harness.js — the frame stepping's bench. **Development only**, deliberately
// outside `tests/run.py`: that suite is Python and dependency-free, and this
// needs a browser to run at all.
//
//     (await import('/align/_harness.js')).run()      // from the page's console
//
// It exists because the path that matters is not exercisable where one usually
// looks. An embedded webview — and any hidden document, `document.hidden` — never
// presents a frame, so `requestVideoFrameCallback` never fires and the page
// falls back to `currentTime`: the stepping cannot be judged there at all. Two
// corrections were shipped blind before this bench named the actual defect.
//
// So it substitutes a fake `<video>` that models a grid of frames and that knows
// how to present **late** and how to report a `mediaTime` **offset** from the
// `currentTime` it was asked for — an MP4 edit list, or a stream whose first
// frame is not at zero. That last one is the whole point: on a −0.05 s offset
// the version that added the two domains burns 29 seeks without the position
// moving an inch, which is the reported symptom, and no other mechanism tried
// reproduces it.
//
// A hidden document also throttles `setTimeout` to ~1 Hz, which made the bench
// interminable; the scheduler below goes through a `MessageChannel`, which is
// not throttled.
export async function run(mod = "./video.js") {
  const chan = new MessageChannel(), q = [];
  chan.port1.onmessage = () => {
    const now = performance.now();
    for (let i = q.length - 1; i >= 0; i--) if (q[i].t <= now) q.splice(i, 1)[0].f();
    if (q.length) chan.port2.postMessage(0);
  };
  const soon = (f, ms = 0) => { q.push({ f, t: performance.now() + ms }); chan.port2.postMessage(0); };
  const raf = window.requestAnimationFrame;
  // 16 ms: the cadence of a 60 Hz screen. The engine's vsync ceiling is a real
  // duration — shortening it would falsify everything about lateness.
  window.requestAnimationFrame = (f) => soon(() => f(performance.now()), 16);

  const { VideoClock } = await import(mod + "?h=" + Date.now());
  const log = [];

  class FakeVideo {
    constructor(pts, o = {}) {
      this.pts = pts; this.duration = pts[pts.length - 1] + 0.05;
      this.paused = true; this._ct = 0; this._cbs = []; this._ls = {};
      this.seekMs = o.seekMs ?? 6; this.presentMs = o.presentMs ?? 8;
      // A hiccup in the compositor: the presentation drags by a few vsyncs.
      // Past that, no implementation can tell it from an absence — rVFC carries
      // no token tying a callback to the seek that caused it.
      this.lateEvery = o.lateEvery ?? 0;
      this.lateMs = o.lateMs ?? 45;
      // The offset between the two domains: `pts` are `currentTime` instants,
      // the reported `mediaTime` is shifted by a constant.
      this.off = o.off ?? 0;
      this.seeks = 0;
    }
    get currentTime() { return this._ct; }
    set currentTime(t) {
      this._ct = t; this.seeks++;
      const late = this.lateEvery && this.seeks % this.lateEvery === 0 ? this.lateMs : 0;
      const frame = this.pts.filter((p) => p <= t + 1e-9).pop() ?? this.pts[0];
      soon(() => this._fire("seeked"), this.seekMs);
      soon(() => this._present(frame), this.seekMs + this.presentMs + late);
    }
    requestVideoFrameCallback(cb) { this._cbs.push(cb); return this._cbs.length; }
    cancelVideoFrameCallback() {}
    addEventListener(t, f) { (this._ls[t] ||= []).push(f); }
    removeEventListener(t, f) { this._ls[t] = (this._ls[t] || []).filter((g) => g !== f); }
    _fire(t) { for (const f of [...(this._ls[t] || [])]) f(); }
    _present(p) {
      const c = this._cbs; this._cbs = [];
      for (const cb of c) cb(0, { mediaTime: +(p + this.off).toFixed(6) });
    }
  }

  const grid = (n, dt, jit = 0) => {
    const out = [0];
    for (let i = 1; i < n; i++) out.push(+(out[i - 1] + dt + (i % 2 ? jit : -jit)).toFixed(6));
    return out;
  };

  // N steps forward then N back must land exactly on the frame started from —
  // read at the reported PTS, never at the request. Everything the page writes
  // as an anchor is that reading.
  async function walk(name, pts, opt, dt, spread) {
    const v = new FakeVideo(pts, opt);
    const s = new VideoClock(v);
    s.rvfc = true; s.dt = dt; s.spread = spread; s.gran = spread[0];
    s.media = v.off; s.ct = 0;              // the 1st frame is displayed, cursor at zero
    const seen = [];
    for (let i = 0; i < 4; i++) { await s.step(1); seen.push(s.media); }
    for (let i = 0; i < 4; i++) { await s.step(-1); seen.push(s.media); }
    const want = [...pts.slice(1, 5), ...pts.slice(0, 4).reverse()].map((x) => x + v.off);
    const bad = seen.filter((m, i) => Math.abs(m - want[i]) > 1e-6).length;
    log.push({ cas: name, ok: bad === 0, seeks: v.seeks,
               lu: seen.map((x) => +x.toFixed(4)), attendu: want.map((x) => +x.toFixed(4)) });
    s.dispose();
  }

  // The cadence is measured through the same closed loop, so it must survive the
  // offset too: a measurement that added the domains would report the offset
  // rather than a frame interval.
  //
  // Measured on a grid that is **not** 30 fps, on purpose: `dt` falls back to
  // 1/30 when nothing can be measured, so asserting 1/30 would pass on a
  // measurement that never happened — which is exactly what the offset case did
  // before this bench was pointed at it.
  // `want` is a **range**, not a value. In VFR there is no cadence to hit: the
  // grid below alternates two interval lengths, so any median is one of them —
  // and what the number is for is sizing a probe, which only needs to sit among
  // the intervals actually present.
  async function measured(name, pts, opt, want) {
    const v = new FakeVideo(pts, opt);
    const s = new VideoClock(v);
    s.rvfc = true;
    await s.measure(4);
    const [lo, hi] = Array.isArray(want) ? want : [want - 2e-3, want + 2e-3];
    const ok = s.dt >= lo && s.dt <= hi;
    log.push({ cas: name, ok, seeks: v.seeks,
               lu: [+s.dt.toFixed(5), +(1 / s.dt).toFixed(2)],
               attendu: [+lo.toFixed(5), +hi.toFixed(5)] });
    s.dispose();
  }

  // `⇧` must still travel ten frames after the arrows have been used — and the
  // arrows are used *alternately*, since comparing a frame with the pinned one
  // is exactly a walk back and forth. The distance a jump travels and the
  // distance a probe starts from are two different numbers; conflating them
  // lets the second erode the first, silently, in the one workflow this page
  // exists for.
  async function jumped(name, pts, opt, alternations) {
    const v = new FakeVideo(pts, opt);
    const s = new VideoClock(v);
    s.rvfc = true;
    await s.measure(4);
    const start = s.media;
    for (let i = 0; i < alternations; i++) { await s.step(1); await s.step(-1); }
    await s.jump(10);
    const travelled = s.media - start;
    const want = pts[10] - pts[0];
    const ok = Math.abs(travelled - want) < 1.5 * (1 / 30);   // ±1 frame: a jump is coarse
    log.push({ cas: name, ok, seeks: v.seeks,
               lu: [+travelled.toFixed(4), `${(travelled / (1 / 30)).toFixed(1)} frames`],
               attendu: [+want.toFixed(4), "10.0 frames"] });
    s.dispose();
  }

  const cfr = grid(40, 1 / 30);
  const vfr = grid(40, 1 / 30, 0.004);     // ±4 ms  : max/min ≈ 1.3 ⇒ tight
  const wild = grid(40, 1 / 30, 0.012);    // ±12 ms : max/min ≈ 2.1 ⇒ a ramp

  await walk("CFR 30 fps", cfr, {}, 1 / 30, [1 / 30, 1 / 30]);
  await walk("CFR · 1 présentation sur 2 en retard de 45 ms", cfr, { lateEvery: 2 }, 1 / 30, [1 / 30, 1 / 30]);
  await walk("CFR · seek lent (120 ms)", cfr, { seekMs: 120 }, 1 / 30, [1 / 30, 1 / 30]);
  await walk("VFR ±4 ms (serré)", vfr, {}, 1 / 30, [1 / 30 - 0.004, 1 / 30 + 0.004]);
  await walk("VFR ±4 ms · 1 sur 2 en retard", vfr, { lateEvery: 2 }, 1 / 30, [1 / 30 - 0.004, 1 / 30 + 0.004]);
  await walk("VFR ±12 ms (rampe)", wild, {}, 1 / 30, [1 / 30 - 0.012, 1 / 30 + 0.012]);
  // The two domains offset by a constant: what an MP4 edit list produces, or a
  // stream whose first frame is not at zero.
  await walk("CFR · mediaTime décalé de +0,5 s", cfr, { off: 0.5 }, 1 / 30, [1 / 30, 1 / 30]);
  await walk("CFR · mediaTime décalé de −0,02 s", cfr, { off: -0.02 }, 1 / 30, [1 / 30, 1 / 30]);
  await walk("VFR ±4 ms · décalé de +0,5 s", vfr, { off: 0.5 }, 1 / 30, [1 / 30 - 0.004, 1 / 30 + 0.004]);
  // −1.5 frame: the value that reproduces the reported pair of symptoms — the
  // forward step never leaving its frame, the backward one crossing two.
  await walk("CFR · mediaTime décalé de −0,05 s", cfr, { off: -0.05 }, 1 / 30, [1 / 30, 1 / 30]);

  const cfr24 = grid(40, 1 / 24);
  await measured("cadence mesurée · CFR 24 fps", cfr24, {}, 1 / 24);
  await measured("cadence mesurée · CFR 24 fps, décalé de −0,05 s", cfr24, { off: -0.05 }, 1 / 24);
  await measured("cadence mesurée · VFR ±4 ms (une des deux durées)", vfr, {},
                 [1 / 30 - 0.005, 1 / 30 + 0.005]);

  await jumped("⇧ = dix frames, sans pas préalable", cfr, {}, 0);
  await jumped("⇧ = dix frames après 4 allers-retours ←/→", cfr, {}, 4);
  await jumped("⇧ = dix frames après 4 allers-retours, décalé de −0,05 s", cfr, { off: -0.05 }, 4);

  window.requestAnimationFrame = raf;
  const bad = log.filter((r) => !r.ok).length;
  console.table(log);
  console.log(bad ? `${bad}/${log.length} CAS ROUGES` : `${log.length} cas verts`);
  return log;
}
