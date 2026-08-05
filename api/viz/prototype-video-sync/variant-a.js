// variant-a.js — PROTOTYPE #8, mise en page A.
//
// « Incrustation permutable » : la 3D occupe toute la scène, la vidéo flotte
// dans un coin — et un clic échange les deux rôles sans changer de page. Le
// pari : pendant une lecture, on regarde *une* image à la fois, l'autre sert de
// contrôle du coin de l'œil ; le rapport de tailles doit donc pouvoir basculer
// aussi vite que l'attention.
//
// Ce qu'elle met en jeu : la vignette est-elle assez grande pour vérifier quoi
// que ce soit, et le HUD survit-il à ce qu'on lui pose une fenêtre dessus ?

const KEY = "proto8.a";

export const name = "Incrustation permutable";
export const hint = "clic sur ⇄ (ou P) pour permuter · glisser le bandeau · " +
                    "coin bas-droit pour redimensionner · V masque la vidéo";

const DEFAULT = { x: null, y: null, w: 340, h: 210, swapped: false, hidden: false };
const MIN_W = 160, MIN_H = 110;   // doivent suivre proto.css (.proto-video-wrap)

export function mount(ctx) {
  const { stage, canvasWrap, videoWrap, videoBar } = ctx;
  const state = { ...DEFAULT, ...readState() };

  stage.classList.add("proto-a");

  const swapBtn = document.createElement("button");
  swapBtn.textContent = "⇄";
  swapBtn.title = "Permuter vidéo et 3D (P)";
  const hideBtn = document.createElement("button");
  hideBtn.textContent = "×";
  hideBtn.title = "Masquer la vidéo (V)";
  videoBar.append(swapBtn, hideBtn);

  function apply() {
    const inset = state.swapped ? canvasWrap : videoWrap;
    const full  = state.swapped ? videoWrap  : canvasWrap;

    full.classList.add("proto-full");
    full.classList.remove("proto-inset");
    inset.classList.add("proto-inset");
    inset.classList.remove("proto-full");
    for (const el of [full, inset]) {
      el.style.left = el.style.top = el.style.width = el.style.height = "";
    }

    // La scène n'a pas toujours de géométrie au moment où la variante se monte
    // (viz.js démarre derrière un `await fetch`). Sans cette garde, `r` vaut
    // 0×0, la vignette part en tailles négatives et se retrouve collée en haut
    // à gauche à sa taille minimale — ce qui se lit comme un choix de maquette
    // alors que c'est un accident de mesure.
    const r = stage.getBoundingClientRect();
    if (r.width < 60 || r.height < 60) { requestAnimationFrame(apply); return; }

    const w = Math.max(MIN_W, Math.min(state.w, r.width - 20));
    const h = Math.max(MIN_H, Math.min(state.h, r.height - 20));
    const x = state.x ?? (r.width - w - 16);
    const y = state.y ?? (r.height - h - 16);
    inset.style.left   = `${Math.max(0, Math.min(x, r.width - w))}px`;
    inset.style.top    = `${Math.max(0, Math.min(y, r.height - h))}px`;
    inset.style.width  = `${w}px`;
    inset.style.height = `${h}px`;

    videoWrap.classList.toggle("proto-hidden", state.hidden && !state.swapped);
    ctx.onLayout();
    save(state);
  }

  // Glisser par le bandeau. La taille, elle, est confiée à `resize: both` —
  // trois lignes de CSS valent mieux qu'une poignée à écrire.
  let drag = null;
  videoBar.addEventListener("pointerdown", (e) => {
    if (e.target !== videoBar && !e.target.classList.contains("grow")) return;
    const box = (state.swapped ? canvasWrap : videoWrap).getBoundingClientRect();
    const r = stage.getBoundingClientRect();
    drag = { dx: e.clientX - box.left, dy: e.clientY - box.top, r };
    videoBar.setPointerCapture(e.pointerId);
  });
  videoBar.addEventListener("pointermove", (e) => {
    if (!drag) return;
    state.x = e.clientX - drag.r.left - drag.dx;
    state.y = e.clientY - drag.r.top  - drag.dy;
    apply();
  });
  const endDrag = () => { drag = null; };
  videoBar.addEventListener("pointerup", endDrag);
  videoBar.addEventListener("pointercancel", endDrag);

  // `resize: both` change la taille sans nous prévenir : on la relit. On refuse
  // les tailles au plancher : elles ne viennent jamais d'un geste, seulement
  // d'un instant où l'élément n'avait pas encore de largeur.
  const ro = new ResizeObserver(() => {
    const el = state.swapped ? canvasWrap : videoWrap;
    if (!el.classList.contains("proto-inset")) return;
    if (el.offsetWidth <= MIN_W && el.offsetHeight <= MIN_H) return;
    state.w = el.offsetWidth; state.h = el.offsetHeight;
    save(state);
    ctx.onLayout();
  });
  ro.observe(videoWrap);
  ro.observe(canvasWrap);

  // La fenêtre change de taille : la vignette doit rester dans la scène.
  const stageRo = new ResizeObserver(() => apply());
  stageRo.observe(stage);

  const swap = () => { state.swapped = !state.swapped; state.hidden = false; apply(); };
  const hide = () => { state.hidden = !state.hidden; apply(); };
  swapBtn.onclick = swap;
  hideBtn.onclick = hide;

  const onKey = (e) => {
    if (e.target.matches("input, textarea, select")) return;
    if (e.key === "p" || e.key === "P") swap();
    if (e.key === "v" || e.key === "V") hide();
  };
  window.addEventListener("keydown", onKey);

  apply();

  return () => {
    window.removeEventListener("keydown", onKey);
    ro.disconnect();
    stageRo.disconnect();
    stage.classList.remove("proto-a");
    for (const el of [canvasWrap, videoWrap]) {
      el.classList.remove("proto-full", "proto-inset", "proto-hidden");
      el.style.left = el.style.top = el.style.width = el.style.height = "";
    }
    swapBtn.remove(); hideBtn.remove();
  };
}

function readState() {
  try { return JSON.parse(localStorage.getItem(KEY)) || {}; } catch { return {}; }
}
function save(s) {
  try { localStorage.setItem(KEY, JSON.stringify(s)); } catch { /* mode privé */ }
}
