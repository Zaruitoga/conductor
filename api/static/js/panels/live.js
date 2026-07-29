// ── Live data: wheel position + per-sensor value cards ──────────────────────
// Two sources, deliberately: the sensor cards come from transport/live_monitor.py
// (what is arriving on the wire), the position from the model's latest frame
// (what we make of it). Keeping them apart is why a computed value can no longer
// masquerade as an incoming stream.

import {
  $, h, setText, setClass, setHidden, keyed,
  makeSpark, updateSpark, fmt, fmtNum,
} from "../dom.js";
import { on, historyOf } from "../store.js";

// Telemetry, shown in the health panel — not a sensor stream.
const NOT_A_SENSOR = new Set(["heartbeat"]);

function createSensor() {
  const spark = makeSpark(64, 18);
  return h("div.sensor", null,
    h("div.sensor__head", null,
      h("span.sensor__name"),
      h("span.spacer"),
      h("span.sensor__rate"),
      spark,
    ),
    h("div.kv"),
  );
}

function kvItem() {
  return h("div.kv__item", null, h("span.kv__k"), h("span.kv__v"));
}

function updateSensor(node, s) {
  const [head, kv] = node.children;
  setText(head.children[0], s.type);
  setText(head.children[2], s.rate != null ? s.rate.toFixed(0) + " Hz" : "—");
  updateSpark(head.children[3], historyOf(s.type), 0, s.rate > 0 ? "ok" : "bad");

  keyed(kv, s.fields, (f) => f[0], kvItem, (item, [k, v]) => {
    setText(item.children[0], k);
    setText(item.children[1], fmt(v));
  });
}

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

  on("live", (live) => {
    if (!live) return;

    const badge = $("live-badge");
    setText(badge, live.connected ? "flux actif" : "aucun flux");
    setClass(badge, "badge badge--" + (live.connected ? "ok" : "bad"));

    const sensors = Object.entries(live.latest || {})
      .filter(([type]) => !NOT_A_SENSOR.has(type))
      .map(([type, vals]) => ({
        type,
        rate: (live.rates || {})[type],
        fields: Object.entries(vals || {}),
      }))
      .sort((a, b) => a.type.localeCompare(b.type));

    keyed($("sensors"), sensors, (s) => s.type, createSensor, updateSensor);
    setHidden($("sensors-empty"), sensors.length > 0);
  });
}
