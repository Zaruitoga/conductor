// switcher.js — PROTOTYPE #8 : la barre qui fait basculer d'une mise en page à
// l'autre. Jaune, flottante, volontairement laide : elle ne doit jamais être
// confondue avec la maquette qu'on juge.
//
// La variante vit dans l'URL (`?variant=B`) : rechargeable, partageable, et
// c'est aussi ce qui répond à « l'état survit-il à un rechargement ? ».
// `?variant=off` éteint tout le prototype — le viz redevient lui-même.

export function mountSwitcher(keys, current, onPick) {
  const bar = document.createElement("div");
  bar.id = "proto-switcher";
  bar.innerHTML = `
    <span class="tag">PROTO #8</span>
    <button data-dir="-1" title="Variante précédente (←)">‹</button>
    <span class="label"></span>
    <button data-dir="1" title="Variante suivante (→)">›</button>`;
  document.body.appendChild(bar);

  const label = bar.querySelector(".label");
  let key = current;

  function setLabel(name) { label.textContent = `${key} — ${name}`; }

  function cycle(dir) {
    const i = keys.indexOf(key);
    key = keys[(i + dir + keys.length) % keys.length];
    const url = new URL(location.href);
    url.searchParams.set("variant", key);
    history.replaceState(null, "", url);
    setLabel(onPick(key));
  }

  bar.querySelectorAll("button").forEach((b) => {
    b.onclick = () => cycle(Number(b.dataset.dir));
  });

  window.addEventListener("keydown", (e) => {
    if (e.target.matches("input, textarea, select, [contenteditable]")) return;
    if (e.key === "ArrowLeft")  cycle(-1);
    if (e.key === "ArrowRight") cycle(1);
  });

  return { setLabel };
}
