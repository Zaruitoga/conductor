// PROTOTYPE JETABLE (#9) — banc d'essai du pas-à-pas. À supprimer avec le reste.
//
// Il existe parce que le panneau intégré ne déclenche JAMAIS
// `requestVideoFrameCallback` : le chemin qui compte n'y est pas exerçable. On
// lui substitue un faux <video> qui modélise une grille de frames et qui sait
// présenter EN RETARD — le scénario exact du défaut signalé.
//
// Un document caché étrangle aussi `setTimeout` à ~1 Hz, ce qui rendait le banc
// interminable ; l'ordonnanceur ci-dessous passe par un MessageChannel, qui ne
// l'est pas.
export async function run(mod = "/engine.js") {
  const chan = new MessageChannel(), q = [];
  chan.port1.onmessage = () => {
    const now = performance.now();
    for (let i = q.length - 1; i >= 0; i--) if (q[i].t <= now) q.splice(i, 1)[0].f();
    if (q.length) chan.port2.postMessage(0);
  };
  const soon = (f, ms = 0) => { q.push({ f, t: performance.now() + ms }); chan.port2.postMessage(0); };
  const raf = window.requestAnimationFrame;
  // 16 ms : la cadence d'un écran à 60 Hz. Le plafond en vsyncs du moteur est
  // une durée réelle — le raccourcir fausserait tout ce qui touche aux retards.
  window.requestAnimationFrame = (f) => soon(() => f(performance.now()), 16);

  const { Stepper } = await import(mod + "?h=" + Date.now());
  const log = [];

  class FakeVideo {
    constructor(pts, o = {}) {
      this.pts = pts; this.duration = pts[pts.length - 1] + 0.05;
      this.paused = true; this._ct = 0; this._cbs = []; this._ls = {};
      this.seekMs = o.seekMs ?? 6; this.presentMs = o.presentMs ?? 8;
      // Un hoquet du compositeur : la présentation traîne de quelques vsyncs.
      // Au-delà, aucune implémentation ne peut la distinguer d'une absence —
      // rVFC ne porte aucun jeton reliant un rappel au seek qui l'a causé.
      this.lateEvery = o.lateEvery ?? 0;
      this.lateMs = o.lateMs ?? 45;
      // Le décalage entre les deux domaines : `pts` sont des instants
      // `currentTime`, le `mediaTime` rapporté est décalé d'une constante.
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

  async function walk(name, pts, opt, dt, spread) {
    const v = new FakeVideo(pts, opt);
    const s = new Stepper(v);
    s.rvfc = true; s.dt = dt; s.spread = spread; s.gran = spread[0];
    s.media = v.off; s.ct = 0;              // la 1re frame est affichée, curseur à zéro
    const seen = [];
    for (let i = 0; i < 4; i++) { await s.step(1); seen.push(s.media); }
    for (let i = 0; i < 4; i++) { await s.step(-1); seen.push(s.media); }
    const want = [...pts.slice(1, 5), ...pts.slice(0, 4).reverse()].map((x) => x + v.off);
    const bad = seen.filter((m, i) => Math.abs(m - want[i]) > 1e-6).length;
    log.push({ cas: name, ok: bad === 0, seeks: v.seeks,
               lu: seen.map((x) => +x.toFixed(4)), attendu: want.map((x) => +x.toFixed(4)) });
  }

  const cfr = grid(40, 1 / 30);
  const vfr = grid(40, 1 / 30, 0.004);     // ±4 ms  : max/min ≈ 1,3 ⇒ régime serré
  const wild = grid(40, 1 / 30, 0.012);    // ±12 ms : max/min ≈ 2,1 ⇒ régime rampe

  await walk("CFR 30 fps", cfr, {}, 1 / 30, [1 / 30, 1 / 30]);
  await walk("CFR · 1 présentation sur 2 en retard de 45 ms", cfr, { lateEvery: 2 }, 1 / 30, [1 / 30, 1 / 30]);
  await walk("CFR · seek lent (120 ms)", cfr, { seekMs: 120 }, 1 / 30, [1 / 30, 1 / 30]);
  await walk("VFR ±4 ms (serré)", vfr, {}, 1 / 30, [1 / 30 - 0.004, 1 / 30 + 0.004]);
  await walk("VFR ±4 ms · 1 sur 2 en retard", vfr, { lateEvery: 2 }, 1 / 30, [1 / 30 - 0.004, 1 / 30 + 0.004]);
  await walk("VFR ±12 ms (rampe)", wild, {}, 1 / 30, [1 / 30 - 0.012, 1 / 30 + 0.012]);
  // Les deux domaines décalés d'une constante : ce que produit une liste
  // d'édition MP4 ou un flux dont la première frame n'est pas à zéro.
  await walk("CFR · mediaTime décalé de +0,5 s", cfr, { off: 0.5 }, 1 / 30, [1 / 30, 1 / 30]);
  await walk("CFR · mediaTime décalé de −0,02 s", cfr, { off: -0.02 }, 1 / 30, [1 / 30, 1 / 30]);
  await walk("VFR ±4 ms · décalé de +0,5 s", vfr, { off: 0.5 }, 1 / 30, [1 / 30 - 0.004, 1 / 30 + 0.004]);
  // −1,5 frame : la valeur qui reproduit la paire de symptômes signalée —
  // l'avant qui ne sort jamais de sa frame, l'arrière qui en franchit deux.
  await walk("CFR · mediaTime décalé de −0,05 s", cfr, { off: -0.05 }, 1 / 30, [1 / 30, 1 / 30]);

  window.requestAnimationFrame = raf;
  return log;
}
