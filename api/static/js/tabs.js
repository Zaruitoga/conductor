// ── Workspace tabs ──────────────────────────────────────────────────────────
// The panel serves three jobs that never happen at once — rigging (ESP config),
// capture (session/rec/playback), creation (scope/params/OSC). They used to
// compete for one page; now each gets a tab and only one is on screen.
//
// Hidden tabs keep their DOM, so every panel module still finds its elements by
// id and renders whether or not its tab is visible. What a module may NOT do is
// measure a hidden element: `clientWidth` is 0 under `hidden`. Anything that
// measures subscribes to onTabChange and skips invisible work (see scope.js).

const TABS = ["scene", "captation", "signaux", "sortie", "config"];
const DEFAULT_TAB = "scene";
const KEY = "conductor_tab";

const listeners = [];
let current = DEFAULT_TAB;

export const activeTab = () => current;

/** Notified with the new tab name after every switch, including the initial one. */
export function onTabChange(fn) {
  listeners.push(fn);
}

export function showTab(name) {
  // A tab renamed between releases must not leave the user on a blank page.
  if (!TABS.includes(name)) name = DEFAULT_TAB;
  current = name;

  for (const t of TABS) {
    const panel = document.getElementById(`tab-${t}`);
    const button = document.getElementById(`tabbtn-${t}`);
    const on = t === name;
    if (panel) panel.hidden = !on;
    if (button) {
      button.setAttribute("aria-selected", on ? "true" : "false");
      // Only the selected tab is in the tab order; arrows move between them.
      button.tabIndex = on ? 0 : -1;
    }
  }

  localStorage.setItem(KEY, name);
  for (const fn of listeners) {
    try { fn(name); } catch (e) { console.error("[tabs]", e); }
  }
}

export function initTabs() {
  const list = document.getElementById("tablist");

  for (const t of TABS) {
    const button = document.getElementById(`tabbtn-${t}`);
    if (button) button.onclick = () => showTab(t);
  }

  // Standard ARIA tablist keyboard behaviour.
  list?.addEventListener("keydown", (e) => {
    const i = TABS.indexOf(current);
    let next = null;
    if (e.key === "ArrowRight") next = TABS[(i + 1) % TABS.length];
    else if (e.key === "ArrowLeft") next = TABS[(i - 1 + TABS.length) % TABS.length];
    else if (e.key === "Home") next = TABS[0];
    else if (e.key === "End") next = TABS[TABS.length - 1];
    if (!next) return;
    e.preventDefault();
    showTab(next);
    document.getElementById(`tabbtn-${next}`)?.focus();
  });

  showTab(localStorage.getItem(KEY) || DEFAULT_TAB);
}

export { TABS };
