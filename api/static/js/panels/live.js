// ── Wheel position ──────────────────────────────────────────────────────────
// What is left of the old "Données live" panel. The per-sensor cards it used to
// carry duplicated the health streams table — same rate, same sparkline, same
// client-side ring buffer — and their raw field chips (gyro_x = 0.737 at 4 Hz)
// were debug output: unreadable while the wheel moves, and nothing in the
// artistic workflow acts on a raw gyro component, which is what model/ is for.
//
// The position is the model's own output (`model.pose`), not a wire value, so
// it is the one thing here the streams table cannot tell you.

import { $, setText, setClass, fmtNum } from "../dom.js";
import { on } from "../store.js";

function renderPose(pose) {
  for (const axis of ["x", "y", "z"]) {
    const el = $("torus-" + axis);
    const v = pose ? pose[axis] : null;
    setText(el, typeof v === "number" ? fmtNum(v, 3) : "—");
    setClass(el.parentElement,
      "tile tile--lg" + (typeof v === "number" ? "" : " tile--empty"));
  }
}

export function initLive() {
  on("model", (model) => {
    if (!model) return;
    renderPose(model.pose);
  });
}
