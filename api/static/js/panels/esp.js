// ── ESP32 configuration ─────────────────────────────────────────────────────
// State comes from the last CFG_ACK (EspConfigurator.state), pushed in the
// snapshot's `esp` field — there is deliberately no GET /api/esp/state.
// The section is change-gated in the store, but that is not enough on its own:
// a row the user has edited and not yet applied must survive an unrelated ACK,
// hence the per-control dirty tracking.

import {
  $, h, setText, setHidden, setAttr, keyed,
  syncControl, trackDirty, clearDirty,
} from "../dom.js";
import { on, state } from "../store.js";
import { api, action, toast } from "../api.js";

export const SLOT_NAMES = [
  "GYRO", "ACCEL", "MAG", "LINEAR_ACCEL",
  "RV", "GEO_RV", "GAME_RV", "ARVR_RV",
];

const DEFAULT_HZ = 50;

// ── Simple slots ────────────────────────────────────────────────────────────

function createSimpleRow() {
  const chk = h("input", { type: "checkbox" });
  const hz = h("input", { type: "number", min: "1", step: "1" });
  const btn = h("button.btn.btn--icon", { type: "button" }, "Appliquer");

  const row = h("div.slot", null,
    h("div.slot__name", null, h("span.idx"), h("span.lbl")),
    h("label.switch", null, chk, "on"),
    h("label.hz-field", null, hz, h("span.small.faint", null, "Hz")),
    btn,
  );

  const markDirty = () => { row.dataset.dirty = "1"; };
  chk.addEventListener("change", markDirty);
  hz.addEventListener("input", markDirty);
  trackDirty(chk, hz);

  btn.onclick = async () => {
    const slot = Number(row.dataset.slot);
    const value = parseFloat(hz.value);
    if (!(value > 0)) { toast("La fréquence doit être > 0", "bad"); return; }
    btn.disabled = true;
    try {
      await api("POST", "/api/esp/simple", { slot, enabled: chk.checked, hz: value });
      clearDirty(chk, hz);
      delete row.dataset.dirty;
      toast(`Slot ${slot} ${SLOT_NAMES[slot]} appliqué`, "ok");
    } catch (e) {
      toast(e.message, "bad");
    } finally {
      btn.disabled = false;
    }
  };

  row._chk = chk;
  row._hz = hz;
  return row;
}

function updateSimpleRow(row, s) {
  row.dataset.slot = s.slot;
  const name = row.firstChild;
  setText(name.children[0], String(s.slot));
  setText(name.children[1], SLOT_NAMES[s.slot] || `slot ${s.slot}`);
  setAttr(row._chk, "aria-label", `Activer ${SLOT_NAMES[s.slot]}`);
  setAttr(row._hz, "aria-label", `Fréquence ${SLOT_NAMES[s.slot]} en Hz`);

  syncControl(row._chk, s.enabled);
  syncControl(row._hz, s.rate_hz || DEFAULT_HZ);
}

function renderSimples(simples) {
  const bySlot = {};
  for (const x of simples || []) bySlot[x.slot] = x;

  // Always the 8 slots, configured or not — the layout must not shift.
  const rows = Array.from({ length: 8 }, (_, slot) => ({
    slot,
    enabled: bySlot[slot] ? bySlot[slot].enabled : false,
    rate_hz: bySlot[slot] ? bySlot[slot].rate_hz : 0,
  }));

  keyed($("simple-rows"), rows, (r) => r.slot, createSimpleRow, updateSimpleRow);
  setHidden($("simple-empty"), true);
}

// ── Super slots ─────────────────────────────────────────────────────────────

function createSuperRow() {
  const del = h("button.btn.btn--icon.btn--danger", { type: "button" }, "Supprimer");
  const row = h("div.slot.slot--wide", null, h("div.slot__name"), del);

  del.onclick = action(async () => {
    const slot = Number(row.dataset.slot);
    const r = await api("DELETE", "/api/esp/super/" + slot);
    return r;
  }, "Super-slot supprimé");

  return row;
}

function updateSuperRow(row, s) {
  row.dataset.slot = s.slot;
  const deps = s.deps.map((d) => SLOT_NAMES[d] || d).join(", ");
  setText(row.firstChild,
    `super[${s.slot}]  [${deps}]  skip=${s.skip_ratio}  ${s.payload_sz} B`);
  setAttr(row.lastChild, "aria-label", `Supprimer le super-slot ${s.slot}`);
}

function renderSupers(supers) {
  const active = (supers || []).filter((s) => s.active);
  keyed($("super-rows"), active, (s) => s.slot, createSuperRow, updateSuperRow);
  setHidden($("super-empty"), active.length > 0);
}

// ── Panel ───────────────────────────────────────────────────────────────────

export const espState = () => state.esp;

export function initEsp() {
  trackDirty($("host-ip"), $("super-slot"), $("super-deps"), $("super-skip"));

  on("esp", (esp) => {
    const badge = $("esp-ack");
    if (!esp) {
      setText(badge, "non acquitté");
      setAttr(badge, "class", "badge badge--warn");
      setText($("esp-host"), "?");
      setHidden($("simple-empty"), false);
      setHidden($("super-empty"), false);
      $("simple-rows").innerHTML = "";
      $("super-rows").innerHTML = "";
      return;
    }
    setText(badge, "acquitté ✓");
    setAttr(badge, "class", "badge badge--ok");
    setText($("esp-host"), esp.host);
    renderSimples(esp.simples);
    renderSupers(esp.supers);
  });

  // ── Commands ──────────────────────────────────────────────────────────────
  $("host-set").onclick = action(async () => {
    const ip = $("host-ip").value.trim() || null;
    const r = await api("POST", "/api/esp/host", { ip });
    clearDirty($("host-ip"));
    return r;
  }, "Host appliqué");

  $("super-add").onclick = action(() => {
    const deps = $("super-deps").value.split(",")
      .map((d) => parseInt(d.trim(), 10))
      .filter((d) => !Number.isNaN(d));
    return api("POST", "/api/esp/super", {
      slot: parseInt($("super-slot").value, 10),
      deps,
      skip: parseInt($("super-skip").value, 10),
    });
  }, "Super-slot configuré");
}
