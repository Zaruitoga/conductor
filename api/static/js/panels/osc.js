// ── OSC bridge: bus → Ableton Live, remapped here, never in code ────────────
// The source picker builds itself from GET /api/model/schema (signals grouped
// by kind, detectors as "événements") and the destination picker from
// GET /api/osc/targets — declaring a signal or a detector is the whole wiring,
// exactly like the scope's picker and the params panel. Index arguments
// (track/device/param) get a real-name <select> once Live has been asked, via
// GET /api/osc/live's cache — a plain number field otherwise, so the panel
// stays usable before Live is even open.

import {
  $, h, setText, setHidden, setDisabled, setClass, keyed,
  syncControl, trackDirty, clearDirty,
} from "../dom.js";
import { on, state } from "../store.js";
import { api, action, toast } from "../api.js";

const KIND_LABEL = {
  geometry: "Géométrie",
  dynamic:  "Dynamique",
  quality:  "Qualité",
};

const ARG_LABEL = {
  track: "Piste", device: "Device", param: "Paramètre",
  send: "Départ", clip: "Clip", scene: "Scène", free: "Valeur",
};

let modelSchema = null;    // {signals: [...], detectors: [...]}
let targets     = [];      // osc/targets.py catalog
let liveTree    = { online: false, tracks: [], devices: {}, parameters: {} };
let routesData  = { routes: [], profile: "default", profiles: [], revision: -1 };
let lastRoutesRevision = -1;

const targetOf = (name) => targets.find((t) => t.name === name);

// ── Loading the three schemas ────────────────────────────────────────────────

async function refreshModelSchema() {
  try {
    modelSchema = await api("GET", "/api/model/schema");
  } catch { /* the panel stays usable without it */ }
}

async function refreshTargets() {
  try {
    targets = (await api("GET", "/api/osc/targets")).targets || [];
  } catch { /* the panel stays usable without it */ }
}

async function refreshLiveTree() {
  try {
    liveTree = await api("GET", "/api/osc/live");
  } catch { /* keep the last known tree */ }
  renderAddArgs();
}

async function refreshRoutes() {
  try {
    routesData = await api("GET", "/api/osc/routes");
    lastRoutesRevision = routesData.revision;
  } catch { /* keep the last known list */ }
  renderRoutes();
  renderProfiles();
}

// ── Add-route form ───────────────────────────────────────────────────────────
// kind -> source options -> target options (filtered: a continuous route can
// only point at a target that accepts a value — an event-only target like
// clip_fire would get a spurious extra argument otherwise) -> arg fields.

function sourceOptions(kind) {
  if (!modelSchema) return [];
  if (kind === "event") {
    return (modelSchema.detectors || []).map((d) => ({
      value: d.name, label: d.name, group: "Événements",
      available: d.available, reason: d.reason,
    }));
  }
  return (modelSchema.signals || []).map((s) => ({
    value: s.name, label: `${s.name} (${s.unit || "—"})`,
    group: KIND_LABEL[s.kind] || s.kind,
    available: s.available, reason: s.reason, range: s.range,
  }));
}

function fillSourceSelect(kind) {
  const sel = $("osc-add-source");
  const groups = new Map();
  for (const opt of sourceOptions(kind)) {
    if (!groups.has(opt.group)) groups.set(opt.group, []);
    groups.get(opt.group).push(opt);
  }
  sel.innerHTML = "";
  for (const [group, opts] of groups) {
    const og = h("optgroup", { label: group });
    for (const opt of opts) {
      og.append(h("option", {
        value: opt.value,
        disabled: !opt.available,
        title: opt.available ? "" : opt.reason,
      }, opt.available ? opt.label : `${opt.label} — ${opt.reason}`));
    }
    sel.append(og);
  }
  fillTargetSelect(kind);
}

function fillTargetSelect(kind) {
  const sel = $("osc-add-target");
  const usable = targets.filter((t) => kind === "event" || !t.event_only);
  sel.innerHTML = "";
  for (const t of usable) {
    sel.append(h("option", { value: t.name }, t.label || t.name));
  }
  onTargetChanged();
}

function argField(kind, index, track, device, previousValue = "") {
  const named =
    kind === "track"  ? liveTree.tracks :
    kind === "device" ? liveTree.devices[track] :
    kind === "param"  ? liveTree.parameters[`${track},${device}`] :
    null;

  const field = h("div.field", null, h("label", null, ARG_LABEL[kind] || kind));
  let input;
  if (named && named.length) {
    input = h("select", { "data-arg": String(index), "data-kind": kind });
    named.forEach((name, i) => input.append(h("option", { value: i }, `${i} — ${name}`)));
  } else {
    input = h("input", {
      type: "number", min: "0", step: "1", "data-arg": String(index), "data-kind": kind,
      placeholder: named ? "aucun découvert" : "index",
    });
  }
  input.value = previousValue;

  // Picking a track/device cascades into the next level's discovery, if it
  // isn't already cached — a real select is only useful once there is data to
  // put in it. Nothing else needs a rebuild: the field's own value is already
  // correct, and rebuilding anyway would replace *every* sibling field with a
  // fresh DOM node, orphaning whichever one the user fills in next — a real
  // bug this comment used to not warn about.
  input.addEventListener("change", async () => {
    const t = Number($("osc-add-args").querySelector('[data-kind="track"]')?.value);
    const d = Number($("osc-add-args").querySelector('[data-kind="device"]')?.value);
    if (kind === "track" && !Number.isNaN(t) && liveTree.devices[t] === undefined) {
      await api("POST", "/api/osc/live/refresh", { level: "devices", track: t });
      await refreshLiveTree();
    } else if (kind === "device" && !Number.isNaN(t) && !Number.isNaN(d)
               && liveTree.parameters[`${t},${d}`] === undefined) {
      await api("POST", "/api/osc/live/refresh", { level: "params", track: t, device: d });
      await refreshLiveTree();
    }
  });

  field.append(input);
  return field;
}

function onTargetChanged() {
  const target = targetOf($("osc-add-target").value);
  const addr = $("osc-add-address");
  if (target && target.name !== "custom") {
    addr.value = target.address || "";
    setDisabled(addr, true);
  } else {
    setDisabled(addr, false);
  }
  renderAddArgs();
}

function renderAddArgs() {
  const target = targetOf($("osc-add-target")?.value);
  const host = $("osc-add-args");
  if (!host) return;

  // Read whatever is already there *before* wiping it — querying the host
  // after clearing it always sees an empty container, which is why a rebuild
  // used to silently reset every field the user had already filled in.
  const previous = [...host.querySelectorAll("[data-arg]")].map((el) => el.value);
  const track  = Number(previous[0]) || 0;
  const device = Number(previous[1]) || 0;

  host.innerHTML = "";
  if (!target) return;

  target.args.forEach((argKind, i) =>
    host.append(argField(argKind, i, track, device, previous[i] ?? "")));
}

async function submitNewRoute() {
  const kind   = $("osc-add-kind").value;
  const source = $("osc-add-source").value;
  const target = targetOf($("osc-add-target").value);
  if (!source) throw new Error("Choisis une source");
  if (!target) throw new Error("Choisis une destination");

  const args = [...$("osc-add-args").querySelectorAll("[data-arg]")]
    .map((el) => parseInt(el.value, 10) || 0);

  const spec = kind === "event"
    ? sourceOptions("event").find((o) => o.value === source)
    : sourceOptions("signal").find((o) => o.value === source);
  const [inMin, inMax] = spec?.range || [0, 1];
  const [outMin, outMax] = target.out || [0, 1];

  await api("POST", "/api/osc/routes", {
    kind, source, target: target.name,
    address: $("osc-add-address").value.trim(),
    args,
    in_min: inMin, in_max: inMax, out_min: outMin, out_max: outMax,
  });
  await refreshRoutes();
}

// ── Route rows ────────────────────────────────────────────────────────────

function routeRow() {
  const enabled = h("input", { type: "checkbox" });
  const del = h("button.btn.btn--icon.btn--danger", { type: "button" }, "✕");
  const test = h("button.btn.btn--icon", { type: "button" }, "▶ test");
  const validBadge = h("span.badge");

  const inMin = h("input", { type: "number", step: "any" });
  const inMax = h("input", { type: "number", step: "any" });
  const outMin = h("input", { type: "number", step: "any" });
  const outMax = h("input", { type: "number", step: "any" });
  const deadband = h("input", { type: "number", step: "any", min: "0" });
  const clamp = h("input", { type: "checkbox" });
  const invert = h("input", { type: "checkbox" });
  const payloadField = h("input", { type: "text", placeholder: "champ (vide = déclenche seul)" });
  const label = h("input", { type: "text", placeholder: "note" });
  const payloadFieldWrapper = h("div.field", null, h("label", null, "champ événement"), payloadField);

  const row = h("div.route", null,
    h("div.route__head", null,
      validBadge,
      h("label.switch.small", null, enabled, "actif"),
      h("span.route__title"),
      h("span.spacer"),
      test,
      del,
    ),
    h("div.route__body", null,
      h("div.field-grid",
        { style: "grid-template-columns:repeat(5,1fr)" },
        h("div.field", null, h("label", null, "in min"), inMin),
        h("div.field", null, h("label", null, "in max"), inMax),
        h("div.field", null, h("label", null, "out min"), outMin),
        h("div.field", null, h("label", null, "out max"), outMax),
        h("div.field", null, h("label", null, "zone morte"), deadband),
      ),
      h("div.row",
        null,
        h("label.switch.small", null, clamp, "clamp"),
        h("label.switch.small", null, invert, "invert"),
      ),
      payloadFieldWrapper,
      h("div.field", null, h("label", null, "note"), label),
    ),
  );

  trackDirty(inMin, inMax, outMin, outMax, deadband, clamp, invert, payloadField, label);

  const push = action(async () => api("PATCH", `/api/osc/routes/${row.dataset.id}`, {
    enabled: enabled.checked,
    in_min: parseFloat(inMin.value), in_max: parseFloat(inMax.value),
    out_min: parseFloat(outMin.value), out_max: parseFloat(outMax.value),
    deadband: parseFloat(deadband.value) || 0,
    clamp: clamp.checked, invert: invert.checked,
    payload_field: payloadField.value.trim() || null,
    label: label.value,
  }), "Route mise à jour");

  const applyAndClear = async () => {
    await push();
    clearDirty(inMin, inMax, outMin, outMax, deadband, payloadField, label);
    await refreshRoutes();
  };
  enabled.onchange = applyAndClear;
  clamp.onchange = applyAndClear;
  invert.onchange = applyAndClear;
  for (const el of [inMin, inMax, outMin, outMax, deadband, payloadField, label]) {
    el.addEventListener("change", applyAndClear);
  }

  del.onclick = action(
    () => api("DELETE", `/api/osc/routes/${row.dataset.id}`).then(refreshRoutes),
    "Route supprimée",
  );
  test.onclick = action(
    () => api("POST", `/api/osc/routes/${row.dataset.id}/test`),
    "Balayage envoyé",
  );

  Object.assign(row, {
    _enabled: enabled, _valid: validBadge, _inMin: inMin, _inMax: inMax,
    _outMin: outMin, _outMax: outMax, _deadband: deadband, _clamp: clamp,
    _invert: invert, _payloadField: payloadField,
    _payloadFieldWrapper: payloadFieldWrapper, _label: label,
  });
  return row;
}

function updateRouteRow(row, r) {
  row.dataset.id = r.id;
  setText(row.querySelector(".route__title"),
    `${r.source} → ${r.address} [${r.args.join(",")}]`);
  setClass(row._valid, "badge " + (r.valid ? "badge--ok" : "badge--warn"));
  setText(row._valid, r.valid ? r.kind : (r.reason || "invalide"));
  row._valid.title = r.valid ? "" : r.reason;

  syncControl(row._enabled, r.enabled);
  syncControl(row._inMin, r.in_min);
  syncControl(row._inMax, r.in_max);
  syncControl(row._outMin, r.out_min);
  syncControl(row._outMax, r.out_max);
  syncControl(row._deadband, r.deadband);
  syncControl(row._clamp, r.clamp);
  syncControl(row._invert, r.invert);
  syncControl(row._payloadField, r.payload_field || "");
  syncControl(row._label, r.label || "");

  setHidden(row._payloadFieldWrapper, r.kind !== "event");
}

function renderRoutes() {
  const list = routesData.routes || [];
  keyed($("osc-routes-list"), list, (r) => r.id, routeRow, updateRouteRow);
  setHidden($("osc-routes-empty"), list.length > 0);
  setText($("osc-routes-count"), String(list.length));
}

// ── Profiles ─────────────────────────────────────────────────────────────────

function renderProfiles() {
  const sel = $("osc-profile-select");
  const names = routesData.profiles || [];
  sel.innerHTML = "";
  for (const n of names) sel.append(h("option", { value: n }, n));
  if (!names.length) sel.append(h("option", { disabled: true }, "(aucun mapping)"));
  setDisabled($("osc-profile-load"), !names.length);
}

// ── Discovery ────────────────────────────────────────────────────────────────

async function discoverTracks() {
  await api("POST", "/api/osc/live/refresh", { level: "tracks" });
  await refreshLiveTree();
  toast("Pistes découvertes", "ok");
}

// ── Runtime (4 Hz push) ──────────────────────────────────────────────────────

function renderRuntime(osc) {
  if (!osc) return;
  syncControl($("osc-enabled"), osc.enabled);
  syncControl($("osc-rate"), osc.rate_hz);
  syncControl($("osc-host"), osc.live.host);
  syncControl($("osc-port"), osc.live.send_port);
  setText($("osc-out-hz"), `${osc.out_hz} msg/s`);
  setText($("osc-sent"), String(osc.sent));
  setClass($("osc-live-badge"), "badge " + (osc.live.online ? "badge--ok" : "badge--bad"));
  setText($("osc-live-badge"), osc.live.online ? "Live connecté" : "Live hors ligne");

  if (osc.routes.revision !== lastRoutesRevision) refreshRoutes();
}

// ── Panel ────────────────────────────────────────────────────────────────────

export function initOsc() {
  trackDirty($("osc-host"), $("osc-port"), $("osc-rate"), $("osc-profile-name"));

  $("osc-add-kind").onchange = (e) => fillSourceSelect(e.target.value);
  $("osc-add-source").onchange = () => fillTargetSelect($("osc-add-kind").value);
  $("osc-add-target").onchange = onTargetChanged;
  $("osc-add-submit").onclick = action(submitNewRoute, "Route ajoutée");

  $("osc-discover-tracks").onclick = () => discoverTracks();

  $("osc-apply-settings").onclick = action(async () => {
    const r = await api("PATCH", "/api/osc/settings", {
      enabled: $("osc-enabled").checked,
      host: $("osc-host").value.trim() || undefined,
      port: parseInt($("osc-port").value, 10) || undefined,
      rate_hz: parseFloat($("osc-rate").value) || undefined,
    });
    clearDirty($("osc-host"), $("osc-port"), $("osc-rate"));
    return r;
  }, "Réglages appliqués");

  $("osc-panic").onclick = action(async () => {
    const r = await api("PATCH", "/api/osc/settings", { enabled: false });
    syncControl($("osc-enabled"), false);
    return r;
  }, "OSC coupé");

  $("osc-profile-save").onclick = action(async () => {
    const name = $("osc-profile-name").value.trim();
    if (!name) throw new Error("Donne un nom au mapping");
    await api("POST", "/api/osc/routes/save", { name });
    clearDirty($("osc-profile-name"));
    await refreshRoutes();
  }, "Mapping sauvegardé");

  $("osc-profile-load").onclick = action(async () => {
    await api("POST", "/api/osc/routes/load", { name: $("osc-profile-select").value });
    await refreshRoutes();
  }, "Mapping chargé");

  on("osc", renderRuntime);

  refreshModelSchema().then(() => fillSourceSelect($("osc-add-kind").value));
  refreshTargets().then(() => fillTargetSelect($("osc-add-kind").value));
  refreshLiveTree();
  refreshRoutes();
}
