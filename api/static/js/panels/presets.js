// ── ESP configuration presets (browser-local) ───────────────────────────────
// Snapshots of the last acknowledged ESP state, replayed as a sequence of
// commands. Deliberately localStorage-only — the backend keeps no preset store.

import { $, h, setText, setHidden, keyed } from "../dom.js";
import { api, toast } from "../api.js";
import { espState } from "./esp.js";

const PRESETS_KEY = "conductor_esp_presets";

const loadPresets = () => {
  try { return JSON.parse(localStorage.getItem(PRESETS_KEY)) || {}; }
  catch { return {}; }
};

const savePresets = (p) => localStorage.setItem(PRESETS_KEY, JSON.stringify(p));

function savePreset(name) {
  if (!name) { toast("Donne un nom au préset", "bad"); return; }
  const esp = espState();
  if (!esp) { toast("État ESP inconnu", "bad"); return; }

  const presets = loadPresets();
  presets[name] = {
    simples: esp.simples.map((s) => ({
      slot: s.slot, enabled: s.enabled, hz: s.rate_hz || 50,
    })),
    supers: esp.supers.filter((s) => s.active).map((s) => ({
      slot: s.slot, deps: s.deps, skip: s.skip_ratio,
    })),
  };
  savePresets(presets);
  render();
  toast("Préset enregistré", "ok");
}

function deletePreset(name) {
  const presets = loadPresets();
  delete presets[name];
  savePresets(presets);
  render();
}

async function applyPreset(name, btn) {
  const preset = loadPresets()[name];
  if (!preset) return;
  btn.disabled = true;
  try {
    for (const s of preset.simples) {
      await api("POST", "/api/esp/simple", { slot: s.slot, enabled: s.enabled, hz: s.hz });
    }
    for (const s of preset.supers) {
      await api("POST", "/api/esp/super", { slot: s.slot, deps: s.deps, skip: s.skip });
    }
    toast("Préset appliqué", "ok");
  } catch (e) {
    toast(e.message, "bad");
  } finally {
    btn.disabled = false;
  }
}

function createRow() {
  const apply = h("button.btn.btn--icon", { type: "button" }, "Appliquer");
  const del = h("button.btn.btn--icon.btn--danger", { type: "button" }, "✕");
  const row = h("div.slot", null, h("div.slot__name"), apply, del);
  row.style.gridTemplateColumns = "1fr auto auto";

  apply.onclick = () => applyPreset(row.dataset.name, apply);
  del.onclick = () => deletePreset(row.dataset.name);
  return row;
}

function updateRow(row, name) {
  row.dataset.name = name;
  setText(row.firstChild, name);
  row.lastChild.setAttribute("aria-label", `Supprimer le préset ${name}`);
  row.children[1].setAttribute("aria-label", `Appliquer le préset ${name}`);
}

function render() {
  const names = Object.keys(loadPresets());
  keyed($("preset-list"), names, (n) => n, createRow, updateRow);
  setHidden($("preset-empty"), names.length > 0);
}

export function initPresets() {
  render();
  $("preset-save").onclick = () => {
    const input = $("preset-name");
    savePreset(input.value.trim());
    input.value = "";
  };
}
