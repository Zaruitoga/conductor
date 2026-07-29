// ── Parameters: tuning without a restart ────────────────────────────────────
// Built entirely from GET /api/model/schema. A `PARAMS.declare(...)` next to the
// code that reads a value is all it takes to get a control here, with the right
// bounds, unit and description — there is no list to keep in sync.
//
// Writes are debounced: dragging a slider would otherwise fire a PATCH per pixel.

import {
  $, h, setText, setHidden, setDisabled, syncControl, trackDirty, clearDirty,
} from "../dom.js";
import { on, state } from "../store.js";
import { api, action, toast } from "../api.js";

const WRITE_DEBOUNCE_MS = 120;

let specs = [];              // ParamSpec list from the schema
let pending = {};            // name -> value awaiting a PATCH
let writeTimer = null;

// ── Writing ─────────────────────────────────────────────────────────────────

function queue(name, value) {
  pending[name] = value;
  clearTimeout(writeTimer);
  writeTimer = setTimeout(flush, WRITE_DEBOUNCE_MS);
}

async function flush() {
  const values = pending;
  pending = {};
  if (!Object.keys(values).length) return;
  try {
    await api("PATCH", "/api/model/params", { values });
  } catch (e) {
    toast(e.message, "bad");
  }
}

// ── One control ─────────────────────────────────────────────────────────────

function paramRow() {
  const slider = h("input.param__slider", { type: "range" });
  const number = h("input.param__number", { type: "number" });

  const row = h("div.param", null,
    h("div.param__head", null,
      h("span.param__name"),
      h("span.spacer"),
      number,
      h("span.param__unit.small.faint"),
    ),
    slider,
    h("div.param__doc.small.faint"),
  );

  const push = (raw) => {
    const spec = row._spec;
    if (!spec) return;
    let v = parseFloat(raw);
    if (!isFinite(v)) return;
    v = Math.max(spec.min, Math.min(spec.max, v));
    // Both controls follow immediately so the pair never disagrees under the
    // finger, even though the value is only confirmed by the next snapshot.
    slider.value = String(v);
    number.value = String(v);
    queue(spec.name, v);
  };

  slider.oninput = (e) => push(e.target.value);
  number.oninput = (e) => push(e.target.value);
  trackDirty(slider, number);

  row._slider = slider;
  row._number = number;
  return row;
}

function updateParamRow(row, spec) {
  row._spec = spec;
  const [head, slider, doc] = row.children;

  setText(head.children[0], spec.name);
  setText(head.children[3], spec.unit || "");
  setText(doc, spec.doc || "");

  // A declared range with no explicit step: 200 positions across it is fine for
  // a slider, and the number field takes over when a value has to be exact.
  const step = spec.step || (spec.max - spec.min) / 200;
  for (const el of [row._number, slider]) {
    el.min = String(spec.min);
    el.max = String(spec.max);
    el.step = String(step);
  }
  syncControl(slider, spec.value);
  syncControl(row._number, spec.value);
}

// ── Rendering ───────────────────────────────────────────────────────────────

function render() {
  const host = $("params-list");
  if (!host) return;

  const groups = new Map();
  for (const spec of specs) {
    const key = spec.group || "divers";
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(spec);
  }

  host.innerHTML = "";
  for (const [group, items] of [...groups].sort()) {
    const section = h("div.param-group", null,
      h("div.panel__section", null, group),
    );
    for (const spec of items) {
      const row = paramRow();
      updateParamRow(row, spec);
      section.append(row);
    }
    host.append(section);
  }
  setHidden($("params-empty"), specs.length > 0);
}

/** Refresh values in place, leaving any control the user is touching alone. */
function syncValues(values) {
  for (const row of document.querySelectorAll("#params-list .param")) {
    const v = values[row._spec?.name];
    if (v === undefined) continue;
    row._spec = { ...row._spec, value: v };
    syncControl(row._slider, v);
    syncControl(row._number, v);
  }
}

function renderProfiles(snapshot) {
  const sel = $("params-profile");
  const names = snapshot.profiles || [];
  if (sel.dataset.names !== JSON.stringify(names)) {
    sel.dataset.names = JSON.stringify(names);
    sel.innerHTML = "";
    for (const n of names) {
      sel.append(h("option", { value: n }, n));
    }
    if (!names.length) {
      sel.append(h("option", { disabled: true }, "(aucun profil)"));
    }
  }
  setDisabled($("params-load"), !names.length);
  setText($("params-revision"),
    `profil ${snapshot.profile} · révision ${snapshot.revision}`);
}

// ── Panel ───────────────────────────────────────────────────────────────────

export async function refreshParams() {
  try {
    const schema = await api("GET", "/api/model/schema");
    specs = schema.params || [];
    render();
    renderProfiles(await api("GET", "/api/model/params"));
  } catch { /* the panel stays usable without it */ }
}

export function initParams() {
  trackDirty($("params-name"));

  $("params-save").onclick = action(async () => {
    const name = $("params-name").value.trim();
    if (!name) throw new Error("Donne un nom au profil");
    const r = await api("POST", "/api/model/params/save", { name });
    clearDirty($("params-name"));
    renderProfiles(r);
    return r;
  }, "Profil sauvegardé");

  $("params-load").onclick = action(async () => {
    const r = await api("POST", "/api/model/params/load",
                        { name: $("params-profile").value });
    syncValues(r.values || {});
    renderProfiles(r);
    return r;
  }, "Profil chargé");

  $("params-reset").onclick = action(async () => {
    const r = await api("POST", "/api/model/params/reset");
    syncValues(r.values || {});
    renderProfiles(r);
    return r;
  }, "Valeurs par défaut restaurées");

  $("model-reset").onclick = action(
    () => api("POST", "/api/model/reset"),
    "Intégrateurs remis à zéro");

  // The revision moves whenever anything changes a value — including another
  // browser tab — so the controls follow rather than drifting apart.
  let lastRevision = -1;
  on("model", async (m) => {
    if (!m?.params) return;
    setText($("params-revision"),
      `profil ${m.params.profile} · révision ${m.params.revision}`);
    if (m.params.revision !== lastRevision) {
      lastRevision = m.params.revision;
      if (specs.length) {
        try {
          syncValues((await api("GET", "/api/model/params")).values || {});
        } catch { /* transient */ }
      }
    }
  });

  refreshParams();
}
