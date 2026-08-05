// variant-c.js — PROTOTYPE #8, mise en page C.
//
// « Superposition » : la vidéo est le fond, la roue du modèle est dessinée
// par-dessus en transparence, sol et grille éteints. Ce n'est plus « voir la
// vidéo *et* la 3D », c'est voir si elles disent la même chose — une variante
// où un désaccord de synchro se lit sans instrument, parce que la roue de
// synthèse décolle visiblement de la roue filmée.
//
// Ce qu'elle met en jeu : c'est la seule des trois qui demande une caméra
// virtuelle placée comme la vraie. Sans champ ni pose de la caméra (que rien
// n'enregistre), la superposition ne peut pas coïncider — le suivi de roue est
// donc coupé et la vue laissée à l'orbite. La variante existe pour qu'on
// tranche si l'idée mérite qu'on aille chercher ces données, ou pas.

const KEY = "proto8.c.opacity";

export const name = "Superposition";
export const hint = "la 3D est dessinée sur la vidéo · curseur d'opacité en haut " +
                    "à droite · suivi de roue coupé (orbite libre)";

export function mount(ctx) {
  const { stage, canvasWrap, videoWrap, three, follow } = ctx;

  stage.classList.add("proto-c");

  // Fond transparent : le canvas laisse voir la vidéo derrière lui. Le
  // renderer est construit avec `alpha: true` par le hook prototype de viz.js.
  const prevAlpha = three.renderer.getClearAlpha();
  three.renderer.setClearAlpha(0);
  const prevGround = three.ground.visible;
  const prevGrid   = three.grid.visible;
  three.ground.visible = false;
  three.grid.visible   = false;

  // Une caméra qui suit la roue au-dessus d'une image fixe donne exactement le
  // contraire de ce qu'on veut comparer.
  const prevFollow = follow.checked;
  follow.checked = false;

  let opacity = readOpacity();
  const box = document.createElement("div");
  box.className = "proto-opacity";
  box.innerHTML = `<span>3D</span><input type="range" min="10" max="100" value="${opacity * 100}">
                   <span class="val">${Math.round(opacity * 100)} %</span>`;
  const slider = box.querySelector("input");
  const val    = box.querySelector(".val");
  stage.appendChild(box);

  const apply = () => {
    canvasWrap.style.opacity = String(opacity);
    val.textContent = `${Math.round(opacity * 100)} %`;
  };
  slider.oninput = () => {
    opacity = slider.value / 100;
    apply();
    try { localStorage.setItem(KEY, String(opacity)); } catch { /* mode privé */ }
  };
  apply();
  ctx.onLayout();

  return () => {
    stage.classList.remove("proto-c");
    box.remove();
    canvasWrap.style.opacity = "";
    videoWrap.style.opacity = "";
    three.renderer.setClearAlpha(prevAlpha);
    three.ground.visible = prevGround;
    three.grid.visible   = prevGrid;
    follow.checked = prevFollow;
  };
}

function readOpacity() {
  const v = parseFloat(localStorage.getItem(KEY));
  return Number.isFinite(v) ? v : 0.75;
}
