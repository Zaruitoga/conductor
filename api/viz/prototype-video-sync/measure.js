// measure.js — PROTOTYPE #8 : l'instrument.
//
// Le ticket demande de **mesurer**, pas de supposer : la dérive réelle sur un
// take entier, le seuil qui corrige sans saccader, le comportement aux vitesses
// extrêmes, et si le décodage vidéo dégrade le fps de la scène ou le débit de
// la socket.
//
// Le HUD du viz affiche déjà paquets/s **et** fps précisément pour que les deux
// modes de défaillance restent distinguables (CLAUDE.md) — c'est donc cet
// instrument-là qu'on prolonge, pas un nouveau.

// 6 000 points ≈ un take de 60 s à 100 Hz : un take entier tient dans l'anneau,
// ce qui est exactement la question posée (« la dérive sur un take entier »).
const CAPACITY = 6000;

export class DriftRecorder {
  constructor(capacity = CAPACITY) {
    this.t     = new Float64Array(capacity);
    this.d     = new Float64Array(capacity);   // dérive de commande (currentTime)
    this.dm    = new Float64Array(capacity);   // dérive observée (mediaTime)
    this.n     = 0;
    this.cap   = capacity;
    this.marks = [];                           // recalages : {t, cause}
  }

  clear() { this.n = 0; this.marks.length = 0; }

  push(t, drift, driftMedia) {
    if (this.n >= this.cap) {          // take plus long que l'anneau : on décale
      this.t.copyWithin(0, 1);
      this.d.copyWithin(0, 1);
      this.dm.copyWithin(0, 1);
      this.n--;
    }
    this.t[this.n]  = t;
    this.d[this.n]  = drift;
    this.dm[this.n] = Number.isFinite(driftMedia) ? driftMedia : NaN;
    this.n++;
  }

  mark(t, cause) {
    this.marks.push({ t, cause });
    if (this.marks.length > 400) this.marks.shift();
  }

  /** Résumé de la passe : c'est ce qui se recopie dans le ticket. */
  summary() {
    let max = 0, sum = 0, sumSq = 0, cnt = 0;
    let maxM = 0, cntM = 0, sumSqM = 0;
    // Les recalages comptent par cause, parce qu'ils ne coûtent pas la même
    // chose : un « reset » est structurel (un par tour de boucle, inévitable et
    // voulu), un « seuil » est une saccade en pleine lecture — c'est celui-là,
    // et lui seul, que le réglage doit faire tomber à zéro.
    const parCause = {};
    for (const m of this.marks) parCause[m.cause] = (parCause[m.cause] || 0) + 1;
    for (let i = 0; i < this.n; i++) {
      const v = this.d[i];
      if (Number.isFinite(v)) {
        max = Math.max(max, Math.abs(v)); sum += v; sumSq += v * v; cnt++;
      }
      const m = this.dm[i];
      if (Number.isFinite(m)) { maxM = Math.max(maxM, Math.abs(m)); sumSqM += m * m; cntM++; }
    }
    return {
      samples:        cnt,
      drift_max_ms:   +(max * 1000).toFixed(1),
      drift_moy_ms:   cnt ? +((sum / cnt) * 1000).toFixed(1) : null,
      drift_rms_ms:   cnt ? +(Math.sqrt(sumSq / cnt) * 1000).toFixed(1) : null,
      media_max_ms:   cntM ? +(maxM * 1000).toFixed(1) : null,
      media_rms_ms:   cntM ? +(Math.sqrt(sumSqM / cntM) * 1000).toFixed(1) : null,
      recalages:      this.marks.length,
      recalages_seuil: parCause.seuil || 0,
      par_cause:      parCause,
      duree_s:        this.n ? +(this.t[this.n - 1] - this.t[0]).toFixed(1) : 0,
    };
  }
}

/**
 * Le coût. Un seul chiffre ne prouve rien — il faut le même chiffre avec et
 * sans décodeur vidéo, sur la même scène. D'où deux accumulateurs et un
 * interrupteur : « détacher la vidéo » libère le décodeur sans toucher au reste.
 */
export class CostMeter {
  constructor() { this.on = []; this.off = []; }

  /** Un échantillon par seconde, tel que le HUD du viz les produit déjà. */
  sample(videoAttached, fps, pps, dropped, total) {
    const bucket = videoAttached ? this.on : this.off;
    bucket.push({ fps, pps, dropped, total });
    if (bucket.length > 120) bucket.shift();
  }

  static _median(xs) {
    if (!xs.length) return null;
    const s = [...xs].sort((a, b) => a - b);
    return s[s.length >> 1];
  }

  stats(bucket) {
    const b = bucket === "on" ? this.on : this.off;
    return {
      n:   b.length,
      fps: CostMeter._median(b.map((x) => x.fps)),
      pps: CostMeter._median(b.map((x) => x.pps)),
    };
  }

  /** Images vidéo perdues par le décodeur — le coût vu de l'autre bout. */
  dropped() {
    const last = this.on[this.on.length - 1];
    if (!last || !last.total) return null;
    return { dropped: last.dropped, total: last.total,
             pct: +((last.dropped / last.total) * 100).toFixed(2) };
  }

  clear() { this.on.length = 0; this.off.length = 0; }
}

// ── Tracé ────────────────────────────────────────────────────────────────────

/**
 * Le tracé montre la dérive de commande (trait plein) et la dérive observée
 * via `mediaTime` (points), plus une barre par recalage. L'échelle est
 * symétrique et bornée par le seuil : on doit voir *le seuil*, sinon on ne
 * peut pas juger s'il est bien placé.
 */
export function drawDrift(canvas, rec, thresholdS) {
  const ctx = canvas.getContext("2d");
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const w = canvas.clientWidth, h = canvas.clientHeight;
  if (!w || !h) return;
  if (canvas.width !== w * dpr) { canvas.width = w * dpr; canvas.height = h * dpr; }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);

  const style = getComputedStyle(document.documentElement);
  const cMuted = style.getPropertyValue("--muted").trim() || "#8a939c";
  const cAcc   = style.getPropertyValue("--accent").trim() || "#4a9eff";
  const cWarn  = style.getPropertyValue("--warn").trim() || "#d29922";
  const cBad   = style.getPropertyValue("--bad").trim() || "#f85149";

  if (!rec.n) {
    ctx.fillStyle = cMuted;
    ctx.font = "11px ui-monospace, monospace";
    ctx.fillText("aucune mesure", 6, h / 2);
    return;
  }

  let peak = thresholdS * 1.6;
  for (let i = 0; i < rec.n; i++) {
    peak = Math.max(peak, Math.abs(rec.d[i]));
    if (Number.isFinite(rec.dm[i])) peak = Math.max(peak, Math.abs(rec.dm[i]));
  }
  const t0 = rec.t[0], t1 = Math.max(rec.t[rec.n - 1], t0 + 1e-6);
  const X = (t) => ((t - t0) / (t1 - t0)) * w;
  const Y = (v) => h / 2 - (v / peak) * (h / 2 - 4);

  ctx.strokeStyle = cMuted; ctx.globalAlpha = 0.5; ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(0, Y(0)); ctx.lineTo(w, Y(0)); ctx.stroke();

  ctx.strokeStyle = cWarn; ctx.setLineDash([3, 3]);
  for (const s of [thresholdS, -thresholdS]) {
    ctx.beginPath(); ctx.moveTo(0, Y(s)); ctx.lineTo(w, Y(s)); ctx.stroke();
  }
  ctx.setLineDash([]); ctx.globalAlpha = 1;

  for (const m of rec.marks) {
    if (m.t < t0) continue;
    ctx.strokeStyle = cBad; ctx.globalAlpha = 0.45;
    ctx.beginPath(); ctx.moveTo(X(m.t), 0); ctx.lineTo(X(m.t), h); ctx.stroke();
  }
  ctx.globalAlpha = 1;

  ctx.strokeStyle = cAcc; ctx.lineWidth = 1.2; ctx.beginPath();
  for (let i = 0; i < rec.n; i++) {
    const x = X(rec.t[i]), y = Y(rec.d[i]);
    i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
  }
  ctx.stroke();

  ctx.fillStyle = cWarn;
  for (let i = 0; i < rec.n; i += 2) {
    if (!Number.isFinite(rec.dm[i])) continue;
    ctx.fillRect(X(rec.t[i]) - 0.5, Y(rec.dm[i]) - 0.5, 1.5, 1.5);
  }

  ctx.fillStyle = cMuted;
  ctx.font = "10px ui-monospace, monospace";
  ctx.fillText(`±${(peak * 1000).toFixed(0)} ms`, 4, 11);
  ctx.fillText(`${(t1 - t0).toFixed(0)} s`, w - 30, h - 4);
}
