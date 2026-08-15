// ── Takes of the active session ─────────────────────────────────────────────
// Notes are edited inline (the old panel used window.prompt). A row being
// edited is marked so the 4 Hz refresh leaves its textarea alone.

import { $, h, setText, setAttr, setHidden, keyed, takeDuration, fmtCount } from "../dom.js";
import { on, state } from "../store.js";
import { api, toast } from "../api.js";
import { refreshSessions } from "./playback.js";

function createRow() {
  // The alignment page is reachable from here and from the viz's take selector:
  // it is a per-take job, so the take's own row is where one goes looking for
  // it. A plain <a> rather than a button — it is a link to a page, it should
  // open in a new tab on ⌘-click like any other.
  const row = h("div.slot.slot--wide.take", { dataset: { editing: "0" } },
    h("div.slot__name"),
    h("div.row",
      { style: "gap:6px" },
      h("a.btn.btn--icon", { title: "Aligner ce take sur sa vidéo" }, "⧉ aligner"),
      h("button.btn.btn--icon", { type: "button" }, "✎ notes"),
    ),
  );

  const editor = h("div.take__notes-editor", null,
    h("textarea", { rows: 3, "aria-label": "Notes du take" }),
    h("div.row", null,
      h("button.btn.btn--primary", { type: "button" }, "Enregistrer"),
      h("button.btn.btn--ghost", { type: "button" }, "Annuler"),
      h("span.small.faint", null, "⌘/Ctrl + Entrée pour valider"),
    ),
  );
  // Full-width under the two grid columns.
  editor.style.gridColumn = "1 / -1";
  row.append(editor);

  const [name, actions] = row.children;
  const [alignLink, editBtn] = actions.children;
  const textarea = editor.firstChild;
  const [saveBtn, cancelBtn] = editor.lastChild.children;

  const open = () => {
    textarea.value = row.dataset.notes || "";
    row.dataset.editing = "1";
    textarea.focus();
  };
  const close = () => { row.dataset.editing = "0"; };

  editBtn.onclick = () => (row.dataset.editing === "1" ? close() : open());
  cancelBtn.onclick = close;

  saveBtn.onclick = async () => {
    const { session, take } = row.dataset;
    saveBtn.disabled = true;
    try {
      await api("PATCH",
        `/api/sessions/${encodeURIComponent(session)}/takes/${encodeURIComponent(take)}`,
        { notes: textarea.value });
      close();
      toast("Notes enregistrées", "ok");
      await refreshSessions();
    } catch (e) {
      toast(e.message, "bad");
    } finally {
      saveBtn.disabled = false;
    }
  };

  textarea.onkeydown = (e) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); saveBtn.click(); }
    else if (e.key === "Escape") { e.preventDefault(); close(); }
  };

  row._name = name;
  row._alignLink = alignLink;
  return row;
}

function updateRow(row, ctx) {
  const t = ctx.take;
  row.dataset.session = ctx.session;
  row.dataset.take = t.name;
  row.dataset.notes = t.notes || "";

  const extras = [
    t.title && t.title !== t.name && t.title,
    t.performer && `perf : ${t.performer}`,
    t.figures && t.figures.length && `figures : ${t.figures.join(", ")}`,
    t.notes && "notes ✓",
  ].filter(Boolean).join(" · ");

  setText(row._name,
    `${t.name} — ${takeDuration(t)} s · ${fmtCount(t.packet_count)} paq.`
    + (extras ? ` · ${extras}` : ""));

  // The query is what /align/ reads at boot and then keeps true itself, so this
  // link stays a valid address after the page has been used.
  setAttr(row._alignLink, "href",
    `/align/?session=${encodeURIComponent(ctx.session)}`
    + `&take=${encodeURIComponent(t.name)}`);
}

export function initTakes() {
  on("session", (sess) => {
    const takes = (sess && sess.takes) || [];
    const items = takes.map((take) => ({ take, session: sess.name }));

    keyed($("takes-list"), items, (i) => i.take.name, createRow, updateRow);
    setHidden($("takes-empty"), takes.length > 0);
    setText($("takes-count"), takes.length);
  });
}

/** Close any open inline editor (Escape handler in shortcuts.js). */
export function closeAllEditors() {
  let closed = false;
  for (const row of $("takes-list").children) {
    if (row.dataset.editing === "1") { row.dataset.editing = "0"; closed = true; }
  }
  return closed;
}

export const takeCount = () => ((state.session && state.session.takes) || []).length;
