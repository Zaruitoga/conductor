// PROTOTYPE JETABLE (#9) — mécanique partagée.
//
// Aucune décision de mise en page ici : seulement le pas-à-pas vidéo (#4) et le
// tracé de la courbe.

export const api = (p, opt) =>
  fetch(p, opt).then((r) => (r.ok ? r.json() : Promise.reject(new Error(r.status))));

// ── Pas-à-pas vidéo ─────────────────────────────────────────────────────────
// #4 : on ne demande jamais au navigateur d'atterrir juste, on lui demande de
// DIRE où il a atterri (`mediaTime` de requestVideoFrameCallback) et on corrige.
//
// Avec une précaution que la première version n'avait pas : `mediaTime` et
// `currentTime` sont DEUX DOMAINES. Rien ne garantit qu'ils partagent une
// origine — une liste d'édition MP4, un flux dont la première frame n'est pas à
// zéro, et une constante s'installe entre les deux. Viser « mediaTime + un
// intervalle » revient alors à viser systématiquement à côté, toujours du même
// côté : en avant on ne sort jamais de la frame courante, en arrière on en
// franchit deux. Ici les demandes vivent dans `ct` (domaine `currentTime`), les
// lectures dans `media` (domaine `mediaTime`), et les deux ne s'additionnent
// jamais.
export class Stepper {
  constructor(video) {
    this.v = video;
    this.dt = 1 / 30;                 // cadence médiane, MESURÉE au chargement
    this.spread = null;               // [min, max] des intervalles de PTS observés
    this.gran = null;                 // le plus court : l'unité de sonde
    this.media = 0;                   // PTS de la frame affichée  (domaine mediaTime)
    this.ct = 0;                      // notre curseur de demande  (domaine currentTime)
    this.supported = "requestVideoFrameCallback" in HTMLVideoElement.prototype;
    this.rvfc = null;                 // null = pas tranché, false = ne rappelle jamais
    this.trace = [];                  // [demandé, lu, présentations] des derniers seeks
    this._seq = 0;                    // nombre de frames présentées depuis le début
    this._ctStep = null;              // l'intervalle d'une frame, mesuré CÔTÉ DEMANDES
    this._fns = [];
    this._busy = false;
    this._pending = 0;
    this._want = null;
    this._seeking = false;
    this.dead = false;
    this._pump();
  }

  on(fn) { this._fns.push(fn); return this; }
  _emit() { for (const f of this._fns) f(this); }
  dispose() { this.dead = true; }

  // Attendre qu'une condition sur la frame AFFICHÉE devienne vraie — au plus une
  // poignée de vsyncs. `requestAnimationFrame` ne se déclenche PAS dans un
  // document caché (et rien n'y est présenté non plus), donc le délai est
  // l'échéance qui compte : sans lui, changer d'onglet en plein pas-à-pas
  // suspend la promesse pour de bon.
  // La fenêtre est courte À DESSEIN. Elle s'ouvre APRÈS `seeked`, donc le
  // navigateur a fini de chercher : une frame qui n'est pas présentée dans les
  // quelques vsyncs qui suivent ne l'est pas parce qu'elle tarde, mais parce
  // qu'il n'y en a pas de nouvelle à montrer — on est resté dans la même frame,
  // ce qui est le cas COURANT d'une rampe. Attendre davantage ferait payer ce
  // cas-là à chaque sonde. Une présentation qui arriverait plus tard est, elle,
  // indiscernable d'une absence : c'est le contrôle de sens de `_one` qui
  // empêche alors de la prendre pour une réponse.
  _settle(ok, ms = 140, vsyncs = 8) {
    return new Promise((res) => {
      let n = 0, done = false;
      const fin = () => { if (!done) { done = true; res(); } };
      const tick = () => { if (!done) (ok() || ++n > vsyncs) ? fin() : requestAnimationFrame(tick); };
      requestAnimationFrame(tick);
      setTimeout(fin, ms);
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

  // UNE chaîne rVFC permanente, réarmée indéfiniment : `media` est TOUJOURS le
  // PTS de la frame affichée — en pause, pendant un seek, en lecture. Un rappel
  // armé PAR seek puis abandonné au bout d'un délai peut arriver après coup, et
  // c'est alors le seek SUIVANT qui le ramasse : il lit la frame d'avant. Ici
  // aucun rappel n'appartient à un seek en particulier, il n'y a qu'un état.
  _pump() {
    if (this.dead) return;
    if (this.supported && this.rvfc !== false) {
      this.v.requestVideoFrameCallback((_n, m) => {
        if (this.dead || this.rvfc === false) return;
        this.media = m.mediaTime; this.rvfc = true; this._seq++;
        this._emit(); this._pump();
      });
      return;
    }
    if (!this.v.paused) { this.media = this.v.currentTime; this._emit(); }
    setTimeout(() => this._pump(), this.v.paused ? 150 : 40);
  }

  // Demander un instant, puis attendre qu'une frame soit présentée. Ne renvoie
  // pas une position : `media` est tenu par la chaîne et fait seul autorité.
  // Le booléen dit seulement si le navigateur a présenté quelque chose — en
  // pause, ne rien présenter signifie qu'on n'a pas changé de frame.
  async _ask(t) {
    const v = this.v;
    const want = Math.max(0, v.duration ? Math.min(t, v.duration - 1e-3) : t);
    if (!this.supported || this.rvfc === false) {
      if (Math.abs(v.currentTime - want) > 1e-6) { v.currentTime = want; await this._seeked(); }
      this.ct = want; this.media = v.currentTime;
      return true;
    }
    const seq0 = this._seq;
    if (Math.abs(v.currentTime - want) > 1e-6) { v.currentTime = want; await this._seeked(); }
    await this._settle(() => this._seq !== seq0);
    this.ct = want;
    if (this.trace.length > 39) this.trace.shift();
    this.trace.push([+want.toFixed(4), +this.media.toFixed(4), this._seq - seq0]);
    return this._seq !== seq0;
  }

  // La cadence du fichier, MESURÉE — elle ne sert qu'à dimensionner la sonde et
  // à afficher un nombre, jamais à calculer une position (#10).
  //
  // Mesurée en PAUSE, en poussant la demande de 8 ms jusqu'à ce que le PTS
  // rapporté change : ce changement est l'événement, pas la valeur visée.
  // Passer par `play()` dépendrait de l'autoplay, refusé ici.
  async measure(n = 5) {
    if (!this.supported) { this.rvfc = false; await this._ask(0); this._emit(); return; }
    await this._ask(0.04);
    if (!this.rvfc) await this._ask(0.2);          // un seek qui change forcément de frame
    if (!this.rvfc) {                              // aucun rappel : navigateur intégré
      this.rvfc = false; this.v.currentTime = 0; this.ct = 0; this.media = 0;
      this._emit(); this._pump();                  // la chaîne rVFC est morte : on relance en repli
      return;
    }
    const dPts = [], dCt = [];
    let t = this.ct, cur = this.media;
    for (let i = 0; i < n; i++) {
      const t0 = t;
      let guard = 0;
      while (Math.abs(this.media - cur) < 1e-9 && guard++ < 40) { t += 0.008; await this._ask(t); }
      if (this.media > cur) { dPts.push(this.media - cur); dCt.push(t - t0); cur = this.media; }
      else break;
    }
    // Un seul intervalle aberrant fausserait l'unité de sonde dont dépend tout
    // le pas-à-pas : on prend la médiane et on ne garde que ce qui gravite
    // autour.
    const trim = (a) => {
      const s = [...a].sort((x, y) => x - y), med = s[s.length >> 1];
      return [med, s.filter((x) => x > med * 0.6 && x < med * 1.6)];
    };
    if (dPts.length) {
      const [med, keep] = trim(dPts);
      this.dt = med;
      this.spread = [keep[0] ?? med, keep[keep.length - 1] ?? med];
      this.gran = this.spread[0];
      this._ctStep = trim(dCt)[0];
    }
    await this._ask(0);
    this._emit();
  }

  // Un seek pendant qu'un autre est en vol laissait la dernière promesse résolue
  // écraser la position — d'où un curseur qui saute à un point cliqué plus tôt.
  // Une seule demande en attente, la plus récente gagne.
  async seek(t) {
    this._want = t;
    if (this._seeking) return;
    this._seeking = true;
    while (this._want != null) {
      const target = this._want; this._want = null;
      await this._ask(target);
      this._emit();
    }
    this._seeking = false;
  }

  // UNE frame, exactement — et sans jamais supposer que les deux domaines se
  // superposent.
  //
  // `ct` est calé juste après une frontière (à moins d'un pas de sonde près).
  // On s'en éloigne par pas de δ jusqu'à ce que le PTS rapporté CHANGE : la
  // première frame atteinte est forcément la voisine, quel que soit le décalage
  // éventuel entre les domaines. Ce qu'on apprend au passage, c'est la distance
  // d'une frame CÔTÉ DEMANDES (`_ctStep`) — de quoi partir juste en dessous la
  // fois suivante, donc une à deux sondes par appui, sans jamais risquer le
  // dépassement qu'un départ trop large provoquerait.
  async _one(dir) {
    const base = this.ct, before = this.media;
    // Le changement doit aller DANS LE SENS demandé. Les deux domaines peuvent
    // être décalés, mais la correspondance reste croissante : une lecture qui
    // recule alors qu'on avance ne peut être qu'un rappel en retard.
    const changed = () => dir > 0 ? this.media > before + 1e-9 : this.media < before - 1e-9;
    if (this.rvfc === false) { await this._ask(base + dir * this.dt); return; }

    const d = (this.gran ?? this.dt) / 4;
    let eps = dir > 0 ? Math.max(d, (this._ctStep ?? 4 * d) - d) : d;
    for (let i = 0; i < 20; i++) {
      await this._ask(base + dir * eps);
      if (changed()) { if (dir > 0) this._ctStep = eps; return; }
      eps += d;
    }
    this.ct = base;                     // rien n'a bougé : le curseur non plus
  }

  // Les appuis s'empilent et se vident une frame à la fois : jamais fusionnés en
  // un saut, sinon la précision se perd exactement quand on la cherche. La file
  // est bornée — pilonner la touche parce que rien ne bouge ne doit pas laisser
  // trente pas à rejouer.
  async step(dir) {
    this._pending = Math.max(-4, Math.min(4, this._pending + Math.sign(dir)));
    if (this._busy) return;
    this._busy = true;
    while (this._pending) {
      const d = Math.sign(this._pending);
      this._pending -= d;
      await this._one(d);
      this._emit();
    }
    this._busy = false;
  }

  // Saut large : approximatif par nature, et c'est très bien — il sert à
  // traverser, la lecture de frame dit ensuite la vérité.
  jump(n) { return this.seek(this.ct + n * (this._ctStep ?? this.dt)); }

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
