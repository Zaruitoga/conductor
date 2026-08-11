// ── Keyboard shortcuts ──────────────────────────────────────────────────────
// Ignored while typing, and while any modifier is held, so they never fight
// with normal text entry or browser shortcuts.
//
// The action shortcuts work from any tab: hidden tabs keep their DOM, so `R`
// starts a recording while you are looking at the OSC routes.

import { $ } from "./dom.js";
import { toggleRecording } from "./panels/recording.js";
import { togglePlayback, stopPlayback, toggleLoop } from "./panels/playback.js";
import { closeAllEditors } from "./panels/takes.js";
import { showTab, TABS } from "./tabs.js";

const TYPING = new Set(["INPUT", "TEXTAREA", "SELECT"]);

function isTyping(el) {
  return !!el && (TYPING.has(el.tagName) || el.isContentEditable);
}

function openHelp() {
  const dlg = $("help-dialog");
  if (!dlg.open) dlg.showModal();
}

export function initShortcuts() {
  $("help-btn").onclick = openHelp;
  $("help-close").onclick = () => $("help-dialog").close();

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      if ($("help-dialog").open) return;      // <dialog> closes itself
      if (closeAllEditors()) e.preventDefault();
      return;
    }

    if (isTyping(e.target)) return;
    if (e.metaKey || e.ctrlKey || e.altKey) return;

    // 1–5 select a workspace, in the order they appear in the tablist.
    if (e.key >= "1" && e.key <= String(TABS.length)) {
      e.preventDefault();
      showTab(TABS[Number(e.key) - 1]);
      return;
    }

    switch (e.key) {
      case "r": case "R":
        e.preventDefault(); toggleRecording(); break;
      case "s": case "S":
        e.preventDefault(); stopPlayback(); break;
      case "l": case "L":
        e.preventDefault(); toggleLoop(); break;
      case "?":
        e.preventDefault(); openHelp(); break;
      case " ":
        // Space would also activate a focused button — let that win.
        if (e.target instanceof Element && e.target.closest("button, a, summary")) return;
        e.preventDefault(); togglePlayback(); break;
      default:
        break;
    }
  });
}
