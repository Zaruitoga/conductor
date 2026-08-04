// ── Session: creation form, metadata, and the strip in the ops column ───────
// The `session` section is change-gated in the store, so this only runs when
// the pushed session tree actually changes.

import {
  $, h, setText, setClass, setHidden, keyed,
  syncControl, trackDirty, clearDirty,
} from "../dom.js";
import { on, state } from "../store.js";
import { api, action } from "../api.js";
import { refreshSessions } from "./playback.js";
import { showTab } from "../tabs.js";

// The comments textarea is refilled only when the session identity changes,
// so an edit in progress is never clobbered by an unrelated metadata update.
let lastSessionName = null;

function kvItem() {
  return h("div.kv__item", null, h("span.kv__k"), h("span.kv__v"));
}

function metaItems(sess) {
  const eq = Object.entries(sess.equipment || {}).filter(([, v]) => v);
  return [
    ["dossier", sess.name],
    ["lieu", sess.location || "—"],
    ...eq,
    ["firmware", sess.firmware_version || "?"],
    ["programme", sess.program_version || "?"],
  ];
}

function renderStrip(sess) {
  const strip = $("session-strip");
  const active = !!sess;

  setClass(strip, "strip area-session" + (active ? "" : " strip--idle"));
  setHidden($("strip-open"), active);
  setHidden($("session-close"), !active);

  if (!active) {
    setText($("strip-title"), "Aucune session");
    $("strip-meta").textContent = "";
    $("strip-meta").append(
      h("span.faint", null, "Ouvre une session pour enregistrer des takes."));
    return;
  }

  setText($("strip-title"), sess.title || sess.name);

  const started = sess.started_at ? sess.started_at.slice(0, 19).replace("T", " ") : "—";
  const items = [
    ["takes", (sess.takes || []).length],
    ["début", started],
    ["lieu", sess.location || "—"],
  ];
  keyed($("strip-meta"), items, (i) => i[0],
    () => h("span", null, h("span.faint"), " ", h("b")),
    (node, [k, v]) => {
      setText(node.children[0], k + " ");
      setText(node.children[1], v);
    });
}

export function initSession() {
  const comments = $("sess-comments-edit");
  trackDirty(comments);

  on("session", (sess) => {
    renderStrip(sess);

    const active = !!sess;
    setHidden($("session-form"), active);
    setHidden($("session-active"), !active);

    if (!active) {
      lastSessionName = null;
      return;
    }

    keyed($("sess-meta"), metaItems(sess), (i) => i[0], kvItem, (node, [k, v]) => {
      setText(node.children[0], k);
      setText(node.children[1], v);
    });

    if (sess.name !== lastSessionName) {
      lastSessionName = sess.name;
      clearDirty(comments);
      comments.value = sess.comments || "";
    } else {
      syncControl(comments, sess.comments || "");
    }
  });

  // ── Commands ──────────────────────────────────────────────────────────────
  // The form is on this tab now, so this is a scroll-and-focus rather than the
  // cross-module reach into the aside's collapsed state it used to be.
  $("strip-open").onclick = () => {
    showTab("captation");
    $("sess-title").focus();
    $("sess-title").scrollIntoView({ block: "nearest" });
  };

  $("session-open").onclick = action(async () => {
    const r = await api("POST", "/api/session/start", {
      title: $("sess-title").value.trim(),
      location: $("sess-location").value.trim(),
      equipment: {
        imu: $("sess-eq-imu").value.trim(),
        camera: $("sess-eq-camera").value.trim(),
        focale: $("sess-eq-focale").value.trim(),
        roue: $("sess-eq-roue").value.trim(),
      },
      comments: $("sess-comments").value,
      firmware_version: $("sess-fw").value.trim(),
    });
    return r;
  }, "Session ouverte");

  $("sess-comments-save").onclick = action(async () => {
    const r = await api("PATCH", "/api/session", { comments: comments.value });
    clearDirty(comments);
    return r;
  }, "Commentaires enregistrés");

  $("session-close").onclick = action(async () => {
    const r = await api("POST", "/api/session/close");
    await refreshSessions();
    return r;
  }, "Session close");
}

export const activeSession = () => state.session;
