// ── Keyboard shortcuts ──────────────────────────────────────────────────────
// Ignored while typing, and while any modifier is held, so they never fight
// with normal text entry or browser shortcuts.

import { $ } from "./dom.js";
import { toggleRecording, putMarker } from "./panels/recording.js";
import { togglePlayback, stopPlayback, toggleLoop } from "./panels/playback.js";
import { closeAllEditors } from "./panels/takes.js";
import { toggleConfig } from "./panels/layout.js";

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

    switch (e.key) {
      case "r": case "R":
        e.preventDefault(); toggleRecording(); break;
      case "m": case "M":
        e.preventDefault(); putMarker(); break;
      case "s": case "S":
        e.preventDefault(); stopPlayback(); break;
      case "l": case "L":
        e.preventDefault(); toggleLoop(); break;
      case "c": case "C":
        e.preventDefault(); toggleConfig(); break;
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
