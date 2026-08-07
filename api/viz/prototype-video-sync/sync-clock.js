// sync-clock.js — PROTOTYPE #8, mais la pièce qui vaut d'être reprise.
//
// La vidéo est esclave de `frame.t` (~100 Hz, précision µs), jamais de
// `playback.elapsed_s` (4 Hz, arrondi au dixième — 100 ms, soit 72° de roue à
// 2 tours/s). C'est l'acquis n°7 de la carte #2 ; ce module est sa mise en
// œuvre littérale, isolée du DOM autour d'elle pour qu'on puisse la juger, la
// mesurer, et la relever telle quelle le jour de l'implémentation.
//
// Le contrat, en un calcul :
//
//     temps_vidéo_visé = frame.t − onset_imu + onset_video
//
// Les deux ancres, jamais leur différence (décision #6). Ce qu'on **ne fait
// pas** compte autant : on n'écrit jamais `currentTime` à chaque frame — un
// seek coûte cher et fait saccader. On l'écrit uniquement au-delà du seuil,
// et sur les deux instants où la timeline change de sens : le `meta` de reset
// (chaque tour de boucle) et une reprise après pause.
//
// Ce module ne connaît ni la mise en page, ni les WebSockets, ni Three.js. Il
// reçoit des nombres et pilote un <video>.

// Combien de secondes on se donne pour résorber une dérive en mode « trim ».
const TRIM_TAU_S  = 1.0;
// Borne du trim : au-delà, ce n'est plus un rattrapage, c'est un ralenti.
const TRIM_MAX    = 0.10;
// Un seek écrit `currentTime` ; la position officielle change tout de suite,
// mais l'image mettra un GOP à suivre. Inutile d'en redemander un aussitôt.
//
// Ce délai se compte en **temps réel**, pas en temps de replay — et ce n'est pas
// une entorse à « tout le temps vient de Tick.t_us », c'est le même
// raisonnement que la cadence d'envoi du pont OSC : ce qu'on plafonne ici est un
// *coût* (un seek prend du thread principal et du décodeur), et un coût se paie
// par seconde de mur, quelle que soit la vitesse du replay. Compté en temps de
// replay, le même 0,25 s valait 62 ms de mur à ×4 — mesuré : 114 recalages en
// 12 s, soit neuf saccades par seconde réelle sur le budget que la page protège.
const SEEK_COOLDOWN_S = 0.25;
const nowS = () => performance.now() / 1000;

export const MODES = {
  seek: "recalage dur seul",
  trim: "trim de vitesse seul",
  both: "trim + recalage dur",
};

export class VideoSyncClock {
  /**
   * @param {HTMLVideoElement} video
   * @param {{thresholdS?: number, mode?: keyof MODES}} opts
   */
  constructor(video, { thresholdS = 0.1, mode = "seek" } = {}) {
    this.video      = video;
    this.thresholdS = thresholdS;
    this.mode       = mode;

    // Alignement : les deux ancres, ou rien. « Pas encore aligné » ne demande
    // aucun champ supplémentaire (décision #6).
    this.onsetImuS   = null;
    this.onsetVideoS = null;

    // Ce que le replay nous dit de lui-même (snapshot 4 Hz — on ne s'en sert
    // que pour l'état, jamais pour le temps).
    this.active = false;
    this.paused = false;
    this.speed  = 1;

    this.lastFrameT   = null;   // dernier frame.t reçu (s)
    this.lastTargetS  = null;   // temps vidéo visé correspondant
    this.needHardSync = true;   // armé au départ, au reset, à la reprise
    this._lastSeekAt  = -Infinity;   // en temps réel : c'est un budget de coût
    this._playPending = false;

    this.stats = {
      drift:        null,   // currentTime − visé, en s (signal de commande)
      driftMedia:   null,   // mediaTime − visé : la dérive vraiment observée
      resyncs:      0,
      lastResync:   null,   // {t, drift, cause}
      trim:         0,      // écart relatif appliqué à playbackRate
      rateAsked:    1,
      rateGot:      1,
      state:        "inactif",
      outOfRange:   false,
    };
  }

  // ── Réglages ───────────────────────────────────────────────────────────────

  /** Les deux ancres ensemble : un alignement est indivisible. */
  setAlignment(onsetImuS, onsetVideoS) {
    this.onsetImuS   = onsetImuS;
    this.onsetVideoS = onsetVideoS;
    this.needHardSync = true;
  }

  setThreshold(s) { this.thresholdS = s; }
  setMode(m)      { this.mode = m; this.stats.trim = 0; }

  get aligned() {
    return Number.isFinite(this.onsetImuS) && Number.isFinite(this.onsetVideoS);
  }

  // ── Les trois entrées ──────────────────────────────────────────────────────

  /**
   * Le `meta` de reset : le modèle repart de zéro (démarrage d'une passe, ou
   * un tour de boucle). La timeline vidéo doit sauter avec lui, sans attendre
   * que la dérive franchisse le seuil — c'est le seul moment où un seek dur
   * est le comportement correct plutôt qu'un pis-aller.
   */
  onReset() {
    this.needHardSync = true;
    this.lastFrameT   = null;
  }

  /** Le snapshot du panneau (4 Hz) : état, vitesse, pause. Jamais le temps. */
  onPlayback(p) {
    const wasActive = this.active;
    this.active = !!(p && p.active);
    this.speed  = (p && p.speed) || 1;

    const paused = !!(p && p.paused);
    const pauseChanged = paused !== this.paused;
    if (pauseChanged) {
      this.paused = paused;
      // Une reprise redémarre la vidéo là où le replay a repris, pas là où
      // elle s'était arrêtée : le seul recalage qu'une pause justifie.
      if (!paused) this.needHardSync = true;
    }

    if (!this.active) {
      this.video.pause();
      this.stats.state = "inactif";
      this.stats.drift = this.stats.driftMedia = null;
      if (wasActive) this.needHardSync = true;
      return;
    }
    if (this.paused) {
      this.video.pause();
      this.stats.state = "en pause";
      // En pause, aucune frame n'arrive : on pose l'image juste une fois, à
      // l'entrée en pause. Le snapshot repasse ici à 4 Hz — rejouer le seek à
      // chaque passage compterait quatre recalages par seconde d'immobilité.
      if (pauseChanged && this.lastTargetS !== null) this._seek(this.lastTargetS, "pause");
    }
  }

  /**
   * Une frame du modèle. ~100 Hz : c'est le chemin chaud, il ne doit rien
   * allouer et n'écrire dans le <video> que lorsqu'il y a une raison.
   * @param {number} t  frame.t, en secondes depuis le premier échantillon du take
   */
  onFrame(t) {
    this.lastFrameT = t;
    const v = this.video;

    if (!this.active || this.paused) return;
    if (!this.aligned)              { this.stats.state = "non aligné"; return; }
    if (!v.duration)                { this.stats.state = "vidéo non chargée"; return; }

    const target = t - this.onsetImuS + this.onsetVideoS;
    this.lastTargetS = target;

    // Hors de la vidéo : le take peut commencer avant que la caméra tourne, ou
    // finir après qu'elle s'est arrêtée. Se taire est plus honnête que
    // d'afficher la première ou la dernière image comme si elle était juste.
    if (target < 0 || target > v.duration) {
      this.stats.outOfRange = true;
      this.stats.state = target < 0 ? "avant la vidéo" : "après la vidéo";
      // Pas de dérive : il n'y a rien à suivre. Laisser la dernière valeur en
      // place la ferait enregistrer comme si elle était encore mesurée.
      this.stats.drift = this.stats.driftMedia = null;
      v.pause();
      return;
    }
    this.stats.outOfRange = false;

    const drift = v.currentTime - target;
    this.stats.drift = drift;

    if (this.needHardSync) {
      this._seek(target, "reset");
      this.needHardSync = false;
    } else if (Math.abs(drift) > this.thresholdS &&
               this.mode !== "trim" &&
               nowS() - this._lastSeekAt > SEEK_COOLDOWN_S) {
      this._seek(target, "seuil");
    } else if (this.mode === "trim" || this.mode === "both") {
      // Rattrapage continu : au lieu de sauter, on joue un peu plus vite ou un
      // peu moins vite jusqu'à ce que l'écart se referme. Ça ne saccade pas —
      // reste à voir si c'est assez rapide, et c'est ce que le prototype juge.
      const trim = Math.max(-TRIM_MAX, Math.min(TRIM_MAX, -drift / TRIM_TAU_S));
      this.stats.trim = trim;
      this._setRate(this.speed * (1 + trim));
    }

    if (this.mode === "seek") { this.stats.trim = 0; this._setRate(this.speed); }

    this.stats.state = "suit le replay";
    // On relit `v.paused` plutôt qu'un drapeau à nous : les contrôles natifs du
    // <video> sont laissés en place pour l'alignement, et une pause faite à la
    // main doit se rattraper toute seule à la frame suivante.
    if (v.paused && !this._playPending) {
      this._playPending = true;
      // `play()` est rejeté à chaque fois que le navigateur reprend la main
      // (onglet caché, économie d'énergie) : sans ce `catch`, chaque reprise
      // laisse une promesse non traitée dans la console et noie les vraies
      // erreurs. Le rejet n'est pas une anomalie, c'est le cas courant.
      v.play().catch(() => {}).finally(() => { this._playPending = false; });
    }
  }

  /**
   * Une image vidéo vient d'être présentée. `mediaTime` porte le PTS réel de
   * cette image (acquis n°10) : `currentTime` relu ne dit pas quelle image est
   * affichée, donc c'est **ici** que se mesure la dérive vraie. Le signal de
   * commande reste `currentTime` — mais si les deux divergent, c'est un
   * résultat du prototype, pas un détail.
   */
  onPresentedFrame(mediaTime) {
    if (this.lastTargetS === null || !this.active || this.paused) return;
    this.stats.driftMedia = mediaTime - this.lastTargetS;
  }

  // ── Écritures dans le <video> ──────────────────────────────────────────────

  _seek(target, cause) {
    this.video.currentTime = Math.max(0, Math.min(target, this.video.duration || target));
    this._lastSeekAt = nowS();
    this.stats.resyncs++;
    this.stats.lastResync = { t: this.lastFrameT, drift: this.stats.drift, cause };
    this.stats.trim = 0;
    this._setRate(this.speed);
  }

  /**
   * `playbackRate = speed`, sans limite artificielle (acquis n°7) — mais le
   * navigateur, lui, en a une. On relit ce qu'il a accepté : un ×8 refusé en
   * silence ferait diverger la vidéo sans que rien ne le dise.
   */
  _setRate(rate) {
    this.stats.rateAsked = rate;
    if (Math.abs(this.video.playbackRate - rate) > 1e-3) {
      try { this.video.playbackRate = rate; } catch { /* hors bornes */ }
    }
    this.stats.rateGot = this.video.playbackRate;
  }
}
