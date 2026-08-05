// variant-b.js — PROTOTYPE #8, mise en page B.
//
// « Côte à côte » : deux images à parts égales, séparées par une poignée. Rien
// ne se recouvre, donc rien ne cache le HUD, et l'œil compare deux vues de même
// taille au lieu d'une grande et d'une petite.
//
// Ce qu'elle met en jeu : sur un écran de portable, deux moitiés font deux
// images étroites — et une roue Cyr filmée en 16/9 posée dans une moitié
// verticale, c'est beaucoup de noir. La question que cette variante pose est
// celle du prix du « rien ne se recouvre ».

const KEY = "proto8.b";

export const name = "Côte à côte";
export const hint = "glisser la poignée centrale · double-clic pour revenir à 50/50";

export function mount(ctx) {
  const { stage, canvasWrap, videoWrap } = ctx;
  let ratio = read();

  stage.classList.add("proto-b");

  const divider = document.createElement("div");
  divider.className = "proto-divider";
  stage.insertBefore(divider, videoWrap);

  function apply() {
    canvasWrap.style.width = `${(ratio * 100).toFixed(1)}%`;
    ctx.onLayout();
  }

  let dragging = false;
  divider.addEventListener("pointerdown", (e) => {
    dragging = true;
    divider.setPointerCapture(e.pointerId);
  });
  divider.addEventListener("pointermove", (e) => {
    if (!dragging) return;
    const r = stage.getBoundingClientRect();
    ratio = Math.max(0.15, Math.min(0.85, (e.clientX - r.left) / r.width));
    apply();
  });
  const stop = () => { if (dragging) { dragging = false; save(ratio); } };
  divider.addEventListener("pointerup", stop);
  divider.addEventListener("pointercancel", stop);
  divider.addEventListener("dblclick", () => { ratio = 0.5; save(ratio); apply(); });

  apply();

  return () => {
    stage.classList.remove("proto-b");
    divider.remove();
    canvasWrap.style.width = "";
  };
}

function read() {
  const v = parseFloat(localStorage.getItem(KEY));
  return Number.isFinite(v) ? v : 0.5;
}
function save(v) {
  try { localStorage.setItem(KEY, String(v)); } catch { /* mode privé */ }
}
