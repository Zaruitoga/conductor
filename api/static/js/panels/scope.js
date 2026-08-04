// ── Scope: full-rate signal traces ──────────────────────────────────────────
// The panel snapshot arrives at 4 Hz, which is one sample in twenty-five at
// 100 Hz — enough to read a number, useless for deciding where a detection
// should fire. So the trace comes from GET /api/model/history instead, which
// returns min/max envelopes at the model's own rate: a one-sample spike still
// reads as a spike, which is the whole point of looking.
//
// The picker builds itself from GET /api/model/schema. Declaring a signal in
// model/signals/ is therefore all it takes to be able to watch it here.

import { $, h, setText, setHidden, setClass, keyed } from "../dom.js";
import { on, state } from "../store.js";
import { api } from "../api.js";
import { onTabChange } from "../tabs.js";

const STORE_KEY = "conductor_scope_signals";
const POLL_MS = 120;

// Distinguishable on the dark surface, and stable per signal so a trace keeps
// its colour between sessions.
const COLOURS = [
  "#4a9eff", "#3fb950", "#d29922", "#f85149", "#bc8cff",
  "#39c5cf", "#ff7b72", "#7ee787", "#ffa657", "#a5d6ff",
];

let schema = null;         // {signals: [...], params: [...]}
let selected = [];         // signal names, in the order they were picked
let windowS = 10;
let autoScale = false;
let latest = null;         // last history payload
let timer = null;

// Every canvas showing the same trace. The full scope on the Signaux tab and
// the read-only strip on Scène share one selection, one palette and one poll —
// the strip is another view of this module's state, not a second scope.
const canvases = [];

function registerCanvas(id, opts = {}) {
  const el = $(id);
  if (el) canvases.push({ el, ...opts });
}

/** A canvas in a hidden tab measures 0 — drawing into it would be meaningless. */
const isVisible = (c) => c.el.clientWidth > 0 && c.el.clientHeight > 0;

const specOf = (name) =>
  (schema?.signals || []).find((s) => s.name === name);

const colourOf = (name) =>
  COLOURS[Math.max(0, selected.indexOf(name)) % COLOURS.length];

// ── Selection, persisted so a tuning session survives a reload ──────────────

function loadSelection() {
  try {
    const raw = JSON.parse(localStorage.getItem(STORE_KEY));
    if (Array.isArray(raw)) return raw;
  } catch { /* first run, or someone edited it by hand */ }
  return ["lean_deg", "speed_ms"];
}

function saveSelection() {
  localStorage.setItem(STORE_KEY, JSON.stringify(selected));
}

// ── Schema and picker ───────────────────────────────────────────────────────

const KIND_LABEL = {
  geometry: "Géométrie — fonctions pures de l'orientation",
  dynamic:  "Dynamique — mémoire courte, constantes réglables",
  quality:  "Qualité — confiance dans les entrées",
};

export async function refreshSchema() {
  try {
    schema = await api("GET", "/api/model/schema");
  } catch {
    return;                       // the trace keeps drawing from what it has
  }
  renderPicker();
  renderLegend();
}

function pickerRow(item) {
  const box = h("input", { type: "checkbox" });
  const row = h("label.pick", null,
    box,
    h("span.pick__name"),
    h("span.pick__unit.small.faint"),
    h("span.pick__why.small.faint"),
  );
  box.onchange = () => {
    const name = row.dataset.key;
    selected = box.checked
      ? [...selected, name]
      : selected.filter((n) => n !== name);
    saveSelection();
    renderPicker();
    renderLegend();
    poll();
  };
  row._box = box;
  return row;
}

function updatePickerRow(row, item) {
  row._box.checked = selected.includes(item.name);
  row._box.disabled = !item.available;
  setText(row.children[1], item.name);
  setText(row.children[2], item.unit || "");
  // An unavailable signal says exactly what to switch on, rather than simply
  // not being there.
  setText(row.children[3], item.available ? "" : item.reason);
  setClass(row, "pick" + (item.available ? "" : " pick--off"));
  row.title = item.doc || "";
  row.style.setProperty("--pick-colour", colourOf(item.name));
}

function renderPicker() {
  const host = $("scope-picker");
  if (!host || !schema) return;

  const groups = [];
  for (const kind of ["geometry", "dynamic", "quality"]) {
    const items = schema.signals.filter((s) => s.kind === kind);
    if (items.length) groups.push({ kind, items });
  }

  keyed(host, groups, (g) => g.kind,
    () => h("div.pick-group", null, h("div.panel__section"), h("div.picks")),
    (node, g) => {
      setText(node.children[0], KIND_LABEL[g.kind] || g.kind);
      keyed(node.children[1], g.items, (i) => i.name, pickerRow, updatePickerRow);
    });
}

// ── Legend ──────────────────────────────────────────────────────────────────

function legendRow() {
  return h("div.legend__item", null,
    h("span.legend__swatch"),
    h("span.legend__name"),
    h("span.legend__value.mono"),
  );
}

function renderLegend() {
  const live = state.model?.signals || {};

  const items = selected.map((name) => {
    const spec = specOf(name);
    const v = live[name];
    return {
      name,
      colour: colourOf(name),
      value: typeof v === "number"
        ? v.toFixed(Math.abs(v) >= 100 ? 0 : 2) + (spec?.unit ? " " + spec.unit : "")
        : "—",
    };
  });

  // Both tabs carry a legend; each keeps its own nodes, reconciled by name.
  for (const [hostId, emptyId] of [
    ["scope-legend", "scope-empty"],
    ["scope-strip-legend", "scope-strip-empty"],
  ]) {
    const host = $(hostId);
    if (!host) continue;
    keyed(host, items, (i) => i.name, legendRow, (node, i) => {
      node.children[0].style.background = i.colour;
      setText(node.children[1], i.name);
      setText(node.children[2], i.value);
    });
    setHidden($(emptyId), items.length > 0);
  }
}

// ── Polling and drawing ─────────────────────────────────────────────────────

async function poll() {
  const visible = canvases.filter(isVisible);
  if (!visible.length) return;          // nobody is looking; see initScope()

  if (!selected.length) { latest = null; drawAll(); return; }

  // One request feeds every canvas, so ask for the widest one's resolution —
  // but never for more columns than the window actually holds samples. Asking
  // for 1500 columns of a 1000-sample window returns empty ones as null, which
  // the gap-aware drawing then renders as a dashed line: a real hole in the
  // signal and an artefact of over-sampling would look identical.
  // Aim for at least two samples per column: the envelope is min/max, so
  // aggregating more samples per column loses nothing — a one-sample spike
  // still reaches the column's max — whereas asking for more columns than
  // there are samples returns empty ones as null, and the gap-aware drawing
  // renders those as dashes. A real hole in the signal must not look the same
  // as an artefact of over-sampling.
  const width = Math.max(...visible.map((c) => c.el.clientWidth));
  const cap = latest?.samples > 0 ? latest.samples / 2 : width;
  const points = Math.max(120, Math.min(2000, Math.round(Math.min(width, cap))));
  try {
    latest = await api("GET",
      `/api/model/history?signals=${selected.join(",")}` +
      `&window=${windowS}&points=${points}`);
  } catch {
    return;
  }
  drawAll();
}

function drawAll() {
  for (const c of canvases) {
    if (isVisible(c)) drawInto(c.el, c);
  }
}

/** Vertical mapping for one signal: declared range, or the visible extremes. */
function scaleFor(name, series) {
  if (!autoScale) {
    const range = specOf(name)?.range;
    if (range && range[1] > range[0]) return { lo: range[0], hi: range[1] };
  }
  let lo = Infinity, hi = -Infinity;
  for (const v of series.min) if (v !== null && v < lo) lo = v;
  for (const v of series.max) if (v !== null && v > hi) hi = v;
  if (!isFinite(lo) || !isFinite(hi)) return { lo: 0, hi: 1 };
  if (hi - lo < 1e-9) { lo -= 0.5; hi += 0.5; }
  const pad = (hi - lo) * 0.08;
  return { lo: lo - pad, hi: hi + pad };
}

function drawInto(canvas, { compact = false } = {}) {
  const w = canvas.clientWidth, hgt = canvas.clientHeight;
  if (!w || !hgt) return;             // hidden tab — nothing meaningful to draw

  // Cap the pixel ratio: past 2 the extra fragments buy nothing on a trace made
  // of thin lines, and this redraws several times a second.
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(hgt * dpr)) {
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(hgt * dpr);
  }

  const g = canvas.getContext("2d");
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, w, hgt);

  // Grid — fewer lines on the short strip, where four would be mostly grid.
  const divisions = compact ? 2 : 4;
  g.strokeStyle = "#2c323b";
  g.lineWidth = 1;
  for (let i = 1; i < divisions; i++) {
    const y = Math.round((hgt * i) / divisions) + 0.5;
    g.beginPath(); g.moveTo(0, y); g.lineTo(w, y); g.stroke();
  }

  if (!latest || !latest.points) return;

  for (const name of selected) {
    const series = latest.signals[name];
    if (!series) continue;
    drawSeries(g, w, hgt, name, series);
  }

  if (compact) return;

  // Time axis label, bottom-right
  g.fillStyle = "#727d8a";
  g.font = "11px ui-monospace, monospace";
  g.textAlign = "right";
  g.fillText(`${windowS} s · ${latest.samples ?? 0} échantillons`, w - 6, hgt - 6);
}

function drawSeries(g, w, hgt, name, series) {
  const { lo, hi } = scaleFor(name, series);
  const n = series.min.length;
  const x = (i) => (i / Math.max(1, n - 1)) * w;
  const y = (v) => hgt - ((v - lo) / (hi - lo)) * hgt;
  const colour = colourOf(name);

  // Filled band between the two envelopes, drawn as contiguous runs so a gap in
  // the signal stays a gap rather than being bridged by a straight line.
  g.fillStyle = colour + "44";
  g.strokeStyle = colour;
  g.lineWidth = 1.25;

  let i = 0;
  while (i < n) {
    while (i < n && series.max[i] === null) i++;
    const start = i;
    while (i < n && series.max[i] !== null) i++;
    const end = i;
    if (end - start < 1) continue;

    g.beginPath();
    for (let k = start; k < end; k++) g.lineTo(x(k), y(series.max[k]));
    for (let k = end - 1; k >= start; k--) g.lineTo(x(k), y(series.min[k]));
    g.closePath();
    g.fill();

    g.beginPath();
    for (let k = start; k < end; k++) {
      const mid = (series.max[k] + series.min[k]) / 2;
      k === start ? g.moveTo(x(k), y(mid)) : g.lineTo(x(k), y(mid));
    }
    g.stroke();
  }
}

// ── Panel ───────────────────────────────────────────────────────────────────

export function initScope() {
  selected = loadSelection();

  registerCanvas("scope-canvas");
  registerCanvas("scope-strip", { compact: true });

  $("scope-window").onchange = (e) => {
    windowS = parseFloat(e.target.value);
    poll();
  };
  $("scope-auto").onchange = (e) => {
    autoScale = e.target.checked;
    drawAll();
  };
  $("scope-toggle").onclick = () => {
    const box = $("scope-picker-box");
    const open = box.hidden;
    setHidden(box, !open);
    $("scope-toggle").setAttribute("aria-expanded", open ? "true" : "false");
  };
  $("scope-clear").onclick = () => {
    selected = [];
    saveSelection();
    renderPicker();
    renderLegend();
    poll();
  };

  // Live values in the legend come from the 4 Hz snapshot — enough for a
  // readout, which is all the legend is.
  on("model", () => renderLegend());

  // The set of available signals changes when the ESP is reconfigured, so the
  // picker has to follow rather than being built once at boot.
  let lastSources = "";
  on("model", (m) => {
    const key = JSON.stringify(m?.sources || {}) + JSON.stringify(m?.unavailable || []);
    if (key !== lastSources) {
      lastSources = key;
      refreshSchema();
    }
  });

  // Fires on tab switches too: `hidden` takes a canvas to 0×0 and back, which
  // is exactly when it needs re-measuring and repainting.
  const ro = new ResizeObserver(() => drawAll());
  for (const c of canvases) ro.observe(c.el);

  // Polling follows visibility. This used to run at 8 Hz for the life of the
  // page whether or not anyone was looking at a trace.
  onTabChange(() => {
    const wanted = canvases.some(isVisible);
    if (wanted && !timer) {
      timer = setInterval(poll, POLL_MS);
      poll();
    } else if (!wanted && timer) {
      clearInterval(timer);
      timer = null;
    }
  });

  refreshSchema();
  renderLegend();
}
