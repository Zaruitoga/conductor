// ── Shell layout: the collapsible configuration column ──────────────────────

import { $, setAttr } from "../dom.js";

const KEY = "conductor_config_collapsed";

function apply(collapsed) {
  $("shell").dataset.configCollapsed = collapsed ? "1" : "0";
  setAttr($("config-toggle"), "aria-expanded", collapsed ? "false" : "true");
  setAttr($("config-toggle"), "title",
    (collapsed ? "Déplier" : "Replier") + " la configuration (C)");
  localStorage.setItem(KEY, collapsed ? "1" : "0");
}

export function toggleConfig() {
  apply($("shell").dataset.configCollapsed !== "1");
}

export function initLayout() {
  apply(localStorage.getItem(KEY) === "1");
  $("config-toggle").onclick = toggleConfig;
}
