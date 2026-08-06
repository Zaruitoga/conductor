// PROTOTYPE JETABLE (#9) — mécanique partagée.
//
// Aucune décision de mise en page ici : seulement le pas-à-pas vidéo (#4) et le
// tracé de la courbe.

export const api = (p, opt) =>
  fetch(p, opt).then((r) => (r.ok ? r.json() : Promise.reject(new Error(r.status))));

// ── Pas-à-pas vidéo ─────────────────────────────────────────────────────────
// #4 : on ne demande jamais au navigateur d'atterrir juste, on lui demande de
// DIRE où il a atterri (`mediaTime` de requestVideoFrameCallback) et on corrige.
export class Stepper {
  constructor(video) {
    this.v = video;
    this.dt = 1 / 30;                 // remplacé par la cadence mesurée
    this.spread = null;               // [min, max] des intervalles observés
    this.media = 0;
    this.tries = 0;
    this.supported = "requestVideoFrameCallback" in HTMLVideoElement.prototype;
    this._fns = [];
    this._busy = false;
    this._pending = 0;
  }

  on(fn) { this._fns.push(fn); return this; }
  _emit() { for (const f of this._fns) f(this); }

  // Le filet de sécurité n'est pas cosmétique : en pause et sans nouvelle frame
  // présentée, rVFC ne rappelle JAMAIS. Sans délai de garde la page se fige.
  _next() {
    return new Promise((res) => {
      if (!this.supported) { setTimeout(() => res(this.v.currentTime), 50); return; }
      let done = false;
      const fin = (t, via) => { if (!done) { done = true; this.via = via; res(t); } };
      this.v.requestVideoFrameCallback((_now, meta) => fin(meta.mediaTime, "rvfc"));
      setTimeout(() => fin(this.v.currentTime, "repli"), 400);
    });
  }

  async _go(t) {
    this.v.currentTime = Math.max(0, Math.min(t, this.v.duration || t));
    return await this._next();
  }

  // La cadence du fichier, MESURÉE — elle ne sert qu'à proposer un pas de
  // navigation, jamais à calculer une position (#10).
  //
  // Mesurée en PAUSE, en poussant le seek de 8 ms jusqu'à ce que le `mediaTime`
  // rapporté change : les points d'atterrissage successifs SONT les frontières
  // de frame. Passer par `play()` dépendrait de l'autoplay, refusé ici.
  async measure(n = 6) {
    const d = [];
    let cur = await this._go(0);
    this.rvfc = this.via === "rvfc";      // faux ⇒ tout ce qui s'affiche est un repli
    for (let i = 0; i < n; i++) {
      let t = cur, m = cur, guard = 0;
      while (m <= cur + 1e-4 && guard++ < 24) { t += 0.008; m = await this._go(t); }
      if (m > cur) d.push(m - cur);
      cur = m;
    }
    if (d.length && this.rvfc) {
      const s = [...d].sort((a, b) => a - b);
      this.dt = s[s.length >> 1];
      this.spread = [s[0], s[s.length - 1]];
    }   // sans rVFC les écarts ne mesurent que notre propre pas de sonde : on garde 1/30
    this.media = await this._go(0);
    this.tries = 1;
    this._emit();
  }

  async seek(t) { this.media = await this._go(t); this.tries = 1; this._emit(); }

  // Boucle fermée : on vise, on lit l'accusé de réception, on renudge.
  async step(n) {
    this._pending += n;
    if (this._busy) return;
    this._busy = true;
    while (this._pending) {
      const k = this._pending; this._pending = 0;
      const start = this.media;
      const eps = this.dt * 0.25;
      let target = start + k * this.dt;
      let m = await this._go(target);
      let guard = 0;
      while (guard < 6 && ((k > 0 && m < start + eps) || (k < 0 && m > start - eps))) {
        guard++;
        target += Math.sign(k) * this.dt * 0.5;
        m = await this._go(target);
      }
      this.media = m; this.tries = guard + 1;
    }
    this._busy = false;
    this._emit();
  }

  // Approximatif par construction : la cadence n'est pas régulière.
  get frameApprox() { return Math.round(this.media / this.dt); }
}

// ── Courbe : norme du gyro brute, tracée sans réduction ─────────────────────
export function drawCurve(cv, o) {
  const dpr = Math.min(devicePixelRatio, 2);
  const w = cv.clientWidth, h = cv.clientHeight;
  cv.width = w * dpr; cv.height = h * dpr;
  const g = cv.getContext("2d");
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, w, h);

  const { curve = [], t0, t1, thr = 0.5 } = o;
  const span = Math.max(t1 - t0, 1e-3);
  let ymax = 1;
  for (const [t, v] of curve) if (t >= t0 && t <= t1 && v > ymax) ymax = v;
  ymax *= 1.15;
  const X = (t) => ((t - t0) / span) * w;
  const Y = (v) => h - (v / ymax) * (h - 4) - 2;

  g.fillStyle = "#11151a"; g.fillRect(0, 0, w, h);

  const stepChoices = [0.02, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30];
  const stp = stepChoices.find((s) => span / s < Math.max(3, w / 70)) ?? 60;
  g.strokeStyle = "#232a31"; g.fillStyle = "#5c666f"; g.font = "10px system-ui";
  for (let t = Math.ceil(t0 / stp) * stp; t <= t1; t += stp) {
    const x = X(t);
    g.beginPath(); g.moveTo(x, 0); g.lineTo(x, h); g.stroke();
    g.fillText(t.toFixed(stp < 1 ? 2 : 1) + "s", x + 3, h - 3);
  }

  // Seuil de silence — la seule constante que l'œil doit pouvoir contrôler.
  g.strokeStyle = "#d29922"; g.setLineDash([4, 3]); g.beginPath();
  g.moveTo(0, Y(thr)); g.lineTo(w, Y(thr)); g.stroke(); g.setLineDash([]);

  g.strokeStyle = "#4a9eff"; g.lineWidth = 1.25; g.beginPath();
  let started = false;
  for (const [t, v] of curve) {
    if (t < t0 - span || t > t1 + span) continue;
    const x = X(t), y = Y(v);
    started ? g.lineTo(x, y) : (g.moveTo(x, y), started = true);
  }
  g.stroke();

  (o.markers ?? []).forEach((m, i) => {
    if (m.t == null || m.t < t0 || m.t > t1) return;
    const x = X(m.t);
    g.strokeStyle = m.color; g.lineWidth = m.wide ? 2 : 1;
    g.setLineDash(m.dashed ? [3, 3] : []);
    g.beginPath(); g.moveTo(x, 0); g.lineTo(x, h); g.stroke(); g.setLineDash([]);
    if (m.label) {
      g.fillStyle = m.color; g.font = m.wide ? "600 10px system-ui" : "10px system-ui";
      g.fillText(m.label, Math.min(x + 4, w - 96), 11 + (m.row ?? i) * 12);
    }
  });
  return { X, toTime: (px) => t0 + (px / w) * span };
}

export const fmt = (s) => (s == null ? "—" : s.toFixed(3) + " s");
