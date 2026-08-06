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
    this.dt = 1 / 30;                 // cadence médiane, MESURÉE au chargement
    this.spread = null;               // [min, max] des intervalles observés
    this.gran = null;                 // le plus court intervalle vu : l'unité sûre
    this.media = 0;                   // PTS de la frame RÉELLEMENT affichée
    this.supported = "requestVideoFrameCallback" in HTMLVideoElement.prototype;
    this.rvfc = null;                 // null = pas encore tranché, false = ne rappelle jamais
    this._fns = [];
    this._busy = false;
    this._pending = 0;
    this._seekTo = null;
    this._seeking = false;
    this._watching = false;
  }

  on(fn) { this._fns.push(fn); return this; }
  _emit() { for (const f of this._fns) f(this); }

  // Attendre qu'une frame soit présentée — au plus une poignée de vsyncs.
  // `requestAnimationFrame` ne se déclenche PAS dans un document caché (et rien
  // n'y est présenté non plus), donc le délai est l'échéance qui compte : sans
  // lui, changer d'onglet en plein pas-à-pas suspend la promesse pour de bon.
  _settle(got) {
    return new Promise((res) => {
      let n = 0, done = false;
      const fin = () => { if (!done) { done = true; res(); } };
      const tick = () => { if (!done) (got() != null || ++n > 8) ? fin() : requestAnimationFrame(tick); };
      requestAnimationFrame(tick);
      setTimeout(fin, 200);
    });
  }

  _seeked() {
    return new Promise((res) => {
      let done = false;
      const fin = () => {
        if (done) return;
        done = true; this.v.removeEventListener("seeked", fin); res();
      };
      this.v.addEventListener("seeked", fin);
      setTimeout(fin, 400);
    });
  }

  // Renvoie le `mediaTime` de la frame réellement PRÉSENTÉE, ou `null` si le
  // navigateur n'en a présenté aucune.
  //
  // C'était le défaut central du pas-à-pas : l'ancien repli renvoyait
  // `currentTime`, c'est-à-dire l'instant DEMANDÉ. Un seek qui retombait dans la
  // frame courante accusait donc un déplacement qui n'avait pas eu lieu — la
  // flèche droite avançait le compteur sans que l'image bouge, et `media`
  // dérivait au milieu d'une frame, si bien que le recul suivant traversait deux
  // frontières. `null` dit la vérité : même frame, on n'a pas bougé.
  async _go(t) {
    const v = this.v;
    const want = Math.max(0, v.duration ? Math.min(t, v.duration - 1e-3) : t);
    const move = () => Math.abs(v.currentTime - want) > 1e-6;
    // « Déclaré » et « rappelle vraiment » sont deux choses : dans un navigateur
    // intégré l'API existe et ne se déclenche jamais. Une fois tranché, on ne
    // repasse plus par elle, sinon aucun seek n'accuserait jamais réception.
    if (!this.supported || this.rvfc === false) {
      if (move()) { v.currentTime = want; await this._seeked(); }
      return v.currentTime;
    }
    let seen = null;
    const h = v.requestVideoFrameCallback((_n, m) => { seen = m.mediaTime; });
    if (move()) { v.currentTime = want; await this._seeked(); }
    await this._settle(() => seen);
    v.cancelVideoFrameCallback(h);
    if (seen != null) this.rvfc = true;
    return seen;
  }

  // La cadence du fichier, MESURÉE — elle ne sert qu'à borner le pas-à-pas et à
  // proposer un pas de navigation, jamais à calculer une position (#10).
  //
  // Mesurée en PAUSE, en poussant le seek de 8 ms jusqu'à ce que le `mediaTime`
  // rapporté change : les points d'atterrissage successifs SONT les frontières
  // de frame. Passer par `play()` dépendrait de l'autoplay, refusé ici.
  async measure(n = 4) {
    if (!this.supported) { this.rvfc = false; await this._go(0); this._emit(); return; }
    let cur = await this._go(0.04);
    if (cur == null) cur = await this._go(0.2);   // un seek qui change forcément de frame
    if (cur == null) {                            // aucun rappel : navigateur intégré
      this.rvfc = false; this.v.currentTime = 0; this.media = 0; this._emit(); return;
    }
    const d = [];
    for (let i = 0; i < n; i++) {
      let t = cur, m = null, guard = 0;
      while ((m == null || m <= cur + 1e-4) && guard++ < 30) { t += 0.008; m = await this._go(t); }
      if (m != null && m > cur) { d.push(m - cur); cur = m; }
    }
    if (d.length) {
      const s = [...d].sort((a, b) => a - b);
      this.dt = s[s.length >> 1];
      this.spread = [s[0], s[s.length - 1]];
      this.gran = s[0];
    }
    this.media = (await this._go(0)) ?? 0;
    this._emit();
  }

  // Pendant la lecture, `media` doit continuer de suivre la frame affichée.
  // Sans cela il restait figé au dernier seek : la lecture se regardait sur
  // `currentTime`, la pause rebasculait sur `media`, et toute la page (réglette,
  // curseur de courbe, lecture du temps) « revenait » au dernier point cliqué —
  // alors que la vidéo, elle, était bien là où on l'avait laissée.
  watch(on) {
    if (this._watching === on) return;
    this._watching = on;
    if (!on) return;
    const pump = () => {
      if (!this._watching || this.v.paused) return;
      if (this.rvfc) {                       // « déclaré » ne suffit pas : il doit rappeler
        this.v.requestVideoFrameCallback((_n, m) => {
          this.media = m.mediaTime; this.rvfc = true; this._emit(); pump();
        });
      } else {                               // ~25 Hz : assez pour une lecture de temps,
        this.media = this.v.currentTime;     // et un timer se déclenche là où rAF est gelé
        this._emit(); setTimeout(pump, 40);
      }
    };
    pump();
  }

  // Un seek pendant qu'un autre est en vol laissait la dernière promesse résolue
  // écraser la position — d'où un curseur qui saute à un point cliqué plus tôt.
  // Une seule demande en attente, la plus récente gagne.
  async seek(t) {
    this._seekTo = t;
    if (this._seeking) return;
    this._seeking = true;
    while (this._seekTo != null) {
      const target = this._seekTo; this._seekTo = null;
      this.media = (await this._go(target)) ?? this.media;
      this._emit();
    }
    this._seeking = false;
  }

  // UNE frame, exactement.
  //
  // Viser `media ± dt` est faux : `dt` est une médiane et la cadence n'est pas
  // régulière, donc dès qu'il surestime l'intervalle réel on atterrit deux ou
  // trois frames plus loin. Deux régimes, tous deux exacts :
  //
  //  • Cadence serrée (max ≤ 1,6·min) — UN seul seek, à 1,75·min. Il dépasse
  //    forcément la frontière suivante (qui est à ≤ 1,6·min) sans jamais
  //    atteindre celle d'après (qui est à ≥ 2·min). C'est prouvé, pas espéré,
  //    et c'est le cas de toutes les vidéos de téléphone.
  //  • Sinon — on part d'un décalage volontairement TROP PETIT et on l'agrandit
  //    jusqu'à ce que la frame change : la première atteinte est la voisine.
  //
  // En arrière, `media` étant la frontière de la frame courante, n'importe quel
  // recul strict tombe dans la précédente ; la rampe reste le filet.
  async _one(dir) {
    const start = this.media;
    const moved = (m) => m != null && (dir > 0 ? m > start + 1e-4 : m < start - 1e-4);
    if (this.rvfc === false) return (await this._go(start + dir * this.dt)) ?? start;

    const g = this.gran ?? this.dt;
    const tight = this.spread && this.spread[1] <= this.spread[0] * 1.6;
    if (dir < 0 || tight) {
      const m = await this._go(start + (dir < 0 ? -g * 0.25 : g * 1.75));
      if (moved(m)) return m;
    }
    let eps = g * 0.5;
    for (let i = 0; i < 12; i++) {
      const m = await this._go(start + dir * eps);
      if (moved(m)) return m;
      eps += g * 0.15;
    }
    return start;
  }

  // Les appuis s'empilent et se vident une frame à la fois : jamais fusionnés en
  // un saut, sinon la précision se perd exactement quand on la cherche.
  async step(dir) {
    this._pending += Math.sign(dir);
    if (this._busy) return;
    this._busy = true;
    while (this._pending) {
      const d = Math.sign(this._pending);
      this._pending -= d;
      this.media = await this._one(d);
      this._emit();
    }
    this._busy = false;
  }

  // Saut large : approximatif par nature (n × la cadence médiane), et c'est très
  // bien — il sert à traverser, la lecture de frame dit ensuite la vérité.
  jump(n) { return this.seek(this.media + n * this.dt); }

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
