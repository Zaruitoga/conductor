// ── ESP health: telemetry tiles + stream conformance table ──────────────────
// Source of truth is transport/esp_health.py (EspHealth.snapshot), which fuses
// heartbeat presence with measured-vs-configured stream rates.

import {
  $, h, setText, setClass, setHidden, setAttr,
  keyed, makeSpark, updateSpark, fmtUptime, fmtCount, fmtNum,
} from "../dom.js";
import { on, historyOf } from "../store.js";

const STATE_BADGE = { online: "ok", degraded: "warn", offline: "bad" };
const STATE_LABEL = { online: "En ligne", degraded: "Dégradé", offline: "Hors ligne" };

const STATUS_LABEL = { ok: "ok", slow: "lent", missing: "absent", unexpected: "inattendu" };
const STATUS_BADGE = { ok: "ok", slow: "warn", missing: "bad", unexpected: "" };

/** One telemetry tile. `tone` colours the border; `meter` adds a fill bar. */
function tile(label, value, { tone = "", unit = "", meter = null } = {}) {
  return { label, value, tone, unit, meter };
}

function buildTiles(health) {
  const hb = health.heartbeat;
  const state = health.state || "offline";

  const tiles = [
    tile("État", STATE_LABEL[state] || state, { tone: STATE_BADGE[state] }),
  ];

  if (!hb) {
    tiles.push(tile("Heartbeat", "aucun", { tone: "bad" }));
    return tiles;
  }

  // Battery: -1 from the ESP is normalised to null backend-side.
  if (hb.battery_pct != null) {
    const pct = hb.battery_pct;
    const tone = pct < 15 ? "bad" : pct < 30 ? "warn" : "";
    tiles.push(tile("Batterie", pct.toFixed(0), { unit: "%", tone, meter: pct }));
  } else {
    tiles.push(tile("Batterie", "—"));
  }

  tiles.push(tile("Uptime", fmtUptime(hb.uptime_ms)));
  tiles.push(tile("RSSI", hb.rssi_dbm ?? "—", { unit: "dBm" }));
  tiles.push(tile("CPU", fmtNum(hb.cpu_temp_c, 1), { unit: "°C" }));
  tiles.push(tile("Paquets", fmtCount(hb.packets_sent)));
  tiles.push(tile("Erreurs UDP", fmtCount(hb.udp_errors), {
    tone: hb.udp_errors > 0 ? "warn" : "",
  }));
  tiles.push(tile("Heartbeat", hb.age_ms, {
    unit: "ms", tone: hb.online ? "" : "bad",
  }));

  return tiles;
}

function createTile() {
  const label = h("div.tile__label");
  // Number and unit are separate spans so tabular-nums applies only to the
  // number, and so each can be updated without touching the other.
  const value = h("div.tile__value", null, h("span.n"), h("span.unit"));
  const meter = h("div.meter", null, h("div.meter__fill"));
  return h("div.tile", null, label, value, meter);
}

function updateTile(node, t) {
  const [label, value, meter] = node.children;
  setText(label, t.label);
  setText(value.children[0], t.value);
  setText(value.children[1], t.unit ? " " + t.unit : "");

  setClass(node, "tile" + (t.tone ? " tile--" + t.tone : ""));

  setHidden(meter, t.meter === null);
  if (t.meter !== null) {
    const fill = meter.firstChild;
    const pct = Math.max(0, Math.min(100, t.meter));
    if (fill.style.width !== pct + "%") fill.style.width = pct + "%";
    setClass(fill, "meter__fill" + (t.tone ? " " + t.tone : ""));
  }
}

// ── Streams table ───────────────────────────────────────────────────────────

function createStreamRow() {
  const spark = makeSpark();
  return h("tr", null,
    h("td.mono"),
    h("td.num"),
    h("td.num"),
    h("td.spark-cell", null, spark),
    h("td", null, h("span.badge")),
  );
}

function updateStreamRow(tr, s) {
  const [type, exp, act, sparkCell, stateCell] = tr.children;
  setText(type, s.type);
  setText(exp, s.expected_hz == null ? "—" : s.expected_hz + " Hz");
  setText(act, s.actual_hz + " Hz");

  const tone = STATUS_BADGE[s.status] || "";
  updateSpark(sparkCell.firstChild, historyOf(s.type), s.expected_hz, tone);

  const badge = stateCell.firstChild;
  setText(badge, STATUS_LABEL[s.status] || s.status);
  setClass(badge, "badge" + (tone ? " badge--" + tone : ""));
}

// ── Network ─────────────────────────────────────────────────────────────────

function kvItem(k, v) {
  return h("div.kv__item", null, h("span.kv__k", null, k), h("span.kv__v", null, v));
}

function renderNet(status) {
  const box = $("esp-net");
  const e = status.esp_net;
  if (!e) return;

  const items = [
    ["hostname", e.hostname],
    ["ip", e.resolved ? e.ip : "non résolu"],
    ["source des données", status.udp.last_esp_ip || "—"],
  ];

  keyed(box, items, (i) => i[0],
    () => kvItem("", ""),
    (node, [k, v]) => {
      setText(node.children[0], k);
      setText(node.children[1], v);
    });
}

export function initHealth() {
  on("health", (health) => {
    if (!health) return;
    const state = health.state || "offline";

    const badge = $("health-badge");
    setText(badge, STATE_LABEL[state] || state);
    setClass(badge, "badge badge--" + (STATE_BADGE[state] || "bad"));

    keyed($("health-tiles"), buildTiles(health), (t) => t.label, createTile, updateTile);

    const streams = health.streams || [];
    const tbody = $("health-streams").tBodies[0];
    keyed(tbody, streams, (s) => s.type, createStreamRow, updateStreamRow);
    setHidden($("health-streams"), !streams.length);
    setHidden($("health-streams-empty"), streams.length > 0);
  });

  on("status", (status) => {
    if (!status) return;
    renderNet(status);
  });
}
