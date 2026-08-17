// layout.js — les trois mises en page de la scène, et ce que la scène en dit.
//
// Aucune ne domine (#19, story 30) : l'incrustation garde la roue en grand et
// pose la vidéo dans un coin, le côte-à-côte donne la même surface aux deux, la
// superposition met la roue de synthèse par-dessus la roue filmée. Chacune sert
// un moment différent, et le choix survit à un rechargement.
//
// Tout le raisonnement est ici, dans une fonction sans DOM : `describe()` prend
// l'état (la mise en page choisie, s'il y a une vidéo, ce que l'horloge de
// synchronisation dit) et rend la description de ce qu'il faut afficher. Les
// deux appliquants — `video.js` pour le cadre et le bandeau, `viz.js` pour la
// caméra et la transparence du rendu — ne décident rien. C'est ce qui rend la
// règle « un take sans vidéo replie le volet **en côte-à-côte** et montre un
// cadre hachuré **ailleurs**, et jamais de badge » vérifiable au banc
// (`_harness.js`) plutôt que de bonne foi.

import { STATE_FOLLOWING, STATE_SCRUBBING, STATE_PAUSED } from "./sync-clock.js";

/**
 * A, B, C de la spec, une entrée chacune — **ajouter une mise en page est une
 * entrée ici et rien d'autre**, ce qui n'est vrai que parce que tout ce qui les
 * distingue est un champ plutôt qu'un test sur leur nom.
 *
 *   video / scene  la place de chacun : inset · half · full · folded
 *   swap           ce que la permutation donne, et le fait qu'elle existe —
 *                  seule l'incrustation en a une : côte à côte les deux ont
 *                  déjà la même surface, superposition n'a qu'un cadre
 *   folds          replie le volet faute de vidéo, plutôt qu'un cadre hachuré :
 *                  le côte-à-côte perdrait la moitié de l'écran sur un cadre vide
 *   follow         le suivi de roue, coupé en superposition (voir `describe`)
 *   transparent    la scène est composée par-dessus l'image
 *
 * Les clés sont ce qui va dans `localStorage`.
 */
const PLACE = {
  incrustation: {
    label: "Incrustation", video: "inset", scene: "full",
    swap: { video: "full", scene: "inset" },
    folds: false, follow: true, transparent: false,
  },
  "cote-a-cote": {
    label: "Côte à côte", video: "half", scene: "half",
    folds: true, follow: true, transparent: false,
  },
  superposition: {
    label: "Superposition", video: "full", scene: "full",
    folds: false, follow: false, transparent: true,
  },
};

export const LAYOUTS = Object.keys(PLACE);
export const DEFAULT_LAYOUT = LAYOUTS[0];

/** Ce que le sélecteur affiche. */
export const label = (name) => PLACE[normalise(name)].label;

const KEY      = "viz.layout";
const SWAP_KEY = "viz.layout.swap";

// Les trois états où **l'image affichée est la bonne**, importés de leur
// producteur pour que les deux ne puissent pas diverger sur une chaîne. Tout le
// reste veut dire que ce qui est à l'écran n'est pas l'image de cet instant —
// et c'est là, et seulement là, que le bandeau doit parler et l'image se retirer.
//
// Deux de ces trois ne se disent pas du tout : suivre le replay est le cas
// nominal (un badge permanent cesse d'être lu en une minute) et une pause pose
// la frame de l'instant et la tient — griser une image *juste* retournerait
// exactement le raisonnement qui justifie de griser les autres. Le balayage,
// lui, est nommé sans être grisé : l'image est la bonne, elle répond simplement
// à une main plutôt qu'à la timeline du take.
const RIGHT_PICTURE = new Set([STATE_FOLLOWING, STATE_SCRUBBING, STATE_PAUSED]);
const SILENT        = new Set([STATE_FOLLOWING, STATE_PAUSED]);

/** Une valeur inconnue (profil ancien, clé bricolée) retombe sur A. */
export function normalise(name) {
  return LAYOUTS.includes(name) ? name : DEFAULT_LAYOUT;
}

/**
 * Ce que le stockage en dit. `store` est passé (jamais `localStorage` en dur)
 * pour que le banc puisse en fournir un faux — et parce qu'un navigateur en
 * navigation privée fait lever l'accès.
 */
export function readLayout(store) {
  try {
    return {
      layout:  normalise(store.getItem(KEY)),
      swapped: store.getItem(SWAP_KEY) === "1",
    };
  } catch {
    return { layout: DEFAULT_LAYOUT, swapped: false };
  }
}

export function saveLayout(store, layout, swapped) {
  try {
    store.setItem(KEY, normalise(layout));
    store.setItem(SWAP_KEY, swapped ? "1" : "0");
  } catch { /* stockage refusé : le choix ne survit pas, le reste marche */ }
}

/**
 * Ce qu'il faut afficher, dérivé en un seul endroit.
 *
 * @param {object} o
 * @param {string} o.layout    la mise en page choisie
 * @param {boolean} o.swapped  l'incrustation est permutée (la scène dans le coin)
 * @param {boolean} o.hasTake  un take est à l'écran (faux en direct, au chargement)
 * @param {boolean} o.hasVideo ce take a une vidéo
 * @param {string} o.state     `clock.stats.state`
 * @param {boolean} o.driven   une lecture ou une main pilote l'image
 * @returns {{layout:string, video:string, scene:string, hatched:boolean,
 *            badge:string, greyed:boolean, follow:boolean, transparent:boolean}}
 */
export function describe({ layout, swapped = false, hasTake = false,
                           hasVideo = false, state = "inactif",
                           driven = false } = {}) {
  const l = normalise(layout);
  const p = PLACE[l];
  // `hasVideo` implique `hasTake` : un take est ce qui *porte* une vidéo, et un
  // appelant qui ne connaît que le second n'a pas à se souvenir du premier.
  const take = hasTake || hasVideo;

  // Aucun take à l'écran — le direct, ou la page qui vient de s'ouvrir. Il n'y a
  // pas d'image *absente* : il n'y a pas de take dont on puisse dire qu'il n'a
  // pas de vidéo. Donc aucun cadre, hachuré ou non, et la mise en page dégénère :
  // rien n'étant composé par-dessus une image, la scène garde son sol — et son
  // suivi de roue, qui n'est coupé qu'à cause de cette composition.
  if (!take) {
    return { layout: l, video: "folded", scene: "full", hatched: false,
             badge: "", greyed: false, follow: true, transparent: false };
  }

  // Le suivi de roue et la transparence sont des propriétés de la mise en page
  // elle-même, vraies avec ou sans vidéo.
  //
  // « Son suivi de roue est coupé » (#28) : la caméra virtuelle de la
  // superposition n'a de sens que posée comme la vraie, et rien n'enregistre la
  // pose de la vraie — une caméra qui suit la roue ferait glisser l'image de
  // synthèse sur une image filmée qui, elle, ne bouge pas.
  //
  // La transparence vaut aussi sans vidéo : le cadre hachuré est *derrière* la
  // scène, et un sol opaque le cacherait entièrement — l'absence ne se verrait
  // nulle part, ce qui est exactement ce que le cadre est là pour dire.
  const fixe = { layout: l, follow: p.follow, transparent: p.transparent };

  // Un take sans vidéo : le volet se replie là où c'est prévu, sous peine de
  // perdre la moitié de l'écran sur un cadre vide. Ailleurs le cadre reste,
  // hachuré — il dit l'absence à l'endroit où l'image serait, ce qu'un rectangle
  // noir ne ferait pas. Rien à permuter alors : il n'y a pas d'image. Et pas de
  // badge : l'absence est déjà nommée (bloc « Lecture », bandeau du cadre), un
  // badge d'état par-dessus ne dirait rien de plus.
  if (!hasVideo) {
    return { ...fixe,
      video: p.folds ? "folded" : p.video,
      scene: "full",
      hatched: !p.folds,
      badge: "", greyed: false,
    };
  }

  // La permutation est une propriété de l'entrée, pas un cas particulier :
  // seule celle qui en déclare une peut être permutée.
  const place = swapped && p.swap ? p.swap : p;

  const named = driven && !SILENT.has(state);

  return { ...fixe,
    video: place.video,
    scene: place.scene,
    hatched: false,
    badge:  named ? state : "",
    greyed: named && !RIGHT_PICTURE.has(state),
  };
}
