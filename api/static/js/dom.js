// ── DOM helpers ─────────────────────────────────────────────────────────────
// Everything here exists to make the 4 Hz snapshot push non-destructive:
// writes are idempotent, lists are reconciled by key rather than rebuilt, and
// controls the user is editing are never overwritten.

export const $ = (id) => document.getElementById(id);

/** Element factory. `h("div.foo", {title: "x"}, "text", childEl)` */
export function h(spec, attrs, ...children) {
  const [tag, ...classes] = spec.split(".");
  const el = document.createElement(tag || "div");
  if (classes.length) el.className = classes.join(" ");
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "dataset") Object.assign(el.dataset, v);
    else if (k in el && k !== "list") el[k] = v;
    else el.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c);
  }
  return el;
}

/** Write text only when it changed — preserves the user's text selection. */
export function setText(el, s) {
  if (!el) return;
  const v = s === null || s === undefined ? "" : String(s);
  if (el.textContent !== v) el.textContent = v;
}

/** Same idea for className. */
export function setClass(el, s) {
  if (el && el.className !== s) el.className = s;
}

export function setAttr(el, name, value) {
  if (!el) return;
  if (value === null || value === undefined || value === false) {
    if (el.hasAttribute(name)) el.removeAttribute(name);
  } else if (el.getAttribute(name) !== String(value)) {
    el.setAttribute(name, String(value));
  }
}

/** Toggle `hidden` without touching the DOM when it's already right. */
export function setHidden(el, hidden) {
  if (el && el.hidden !== !!hidden) el.hidden = !!hidden;
}

export function setDisabled(el, disabled) {
  if (el && el.disabled !== !!disabled) el.disabled = !!disabled;
}

/**
 * Reconcile `container`'s children against `items`, keyed.
 * Nodes are created once and updated in place, so hover, focus, text
 * selection and inline editing all survive the refresh.
 */
export function keyed(container, items, keyOf, create, update) {
  if (!container) return;
  // Unkeyed children are static placeholders from the HTML; collect them so
  // they get removed on the first render rather than lingering forever.
  const seen = new Map();
  const unkeyed = [];
  for (const node of container.children) {
    if (node.dataset.key === undefined) unkeyed.push(node);
    else seen.set(node.dataset.key, node);
  }
  for (const node of unkeyed) node.remove();

  let i = 0;
  for (const item of items) {
    const k = String(keyOf(item));
    let node = seen.get(k);
    if (node) {
      seen.delete(k);
    } else {
      node = create(item);
      node.dataset.key = k;
    }
    update(node, item);
    if (container.children[i] !== node) {
      container.insertBefore(node, container.children[i] || null);
    }
    i++;
  }

  for (const stale of seen.values()) stale.remove();
}

// ── Protecting in-flight user input ─────────────────────────────────────────
// The panel receives a full snapshot 4× per second. Any control the user is
// typing into, or has changed but not yet submitted, must be left alone.

/** Write a value into a control unless it is focused or dirty. */
export function syncControl(el, value) {
  if (!el) return;
  if (el === document.activeElement) return;
  if (el.dataset.dirty === "1") return;
  if (el.type === "checkbox") {
    if (el.checked !== !!value) el.checked = !!value;
  } else {
    const v = value === null || value === undefined ? "" : String(value);
    if (el.value !== v) el.value = v;
  }
}

/** Mark a control dirty as soon as the user touches it. */
export function trackDirty(...els) {
  for (const el of els) {
    if (!el || el.dataset.tracked === "1") continue;
    el.dataset.tracked = "1";
    const mark = () => { el.dataset.dirty = "1"; };
    el.addEventListener("input", mark);
    el.addEventListener("change", mark);
  }
}

/** Called once a command has been accepted — the snapshot is now authoritative. */
export function clearDirty(...els) {
  for (const el of els) {
    if (el) delete el.dataset.dirty;
  }
}

// ── Sparklines ──────────────────────────────────────────────────────────────

const SPARK_W = 120;
const SPARK_H = 24;

/**
 * Points for a <polyline>, scaled against `ref` (the expected rate) so the
 * curve reads as conformance rather than auto-scaled noise.
 */
export function sparkPoints(buf, ref = 0, w = SPARK_W, h = SPARK_H) {
  if (!buf || buf.length < 2) return "";
  const max = Math.max(ref || 0, ...buf, 1) * 1.1;
  const n = buf.length;
  const pts = new Array(n);
  for (let i = 0; i < n; i++) {
    const x = (i / (n - 1)) * w;
    const y = h - (buf[i] / max) * h;
    pts[i] = `${x.toFixed(1)},${y.toFixed(1)}`;
  }
  return pts.join(" ");
}

/** The y of the reference line, in the same scale as sparkPoints(). */
export function sparkRefY(buf, ref, h = SPARK_H) {
  if (!ref || !buf || !buf.length) return null;
  const max = Math.max(ref, ...buf, 1) * 1.1;
  return (h - (ref / max) * h).toFixed(1);
}

/** Build a sparkline <svg> with an optional dashed reference line. */
export function makeSpark(w = SPARK_W, h = SPARK_H) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("class", "spark");
  svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
  svg.setAttribute("preserveAspectRatio", "none");
  svg.setAttribute("aria-hidden", "true");

  const ref = document.createElementNS(ns, "line");
  ref.setAttribute("class", "spark__ref");
  ref.setAttribute("x1", "0");
  ref.setAttribute("x2", String(w));
  svg.append(ref);

  const line = document.createElementNS(ns, "polyline");
  svg.append(line);

  return svg;
}

/** Update a sparkline built by makeSpark(). */
export function updateSpark(svg, buf, ref, statusClass) {
  if (!svg) return;
  const [refLine, poly] = [svg.firstChild, svg.lastChild];
  setAttr(poly, "points", sparkPoints(buf, ref));

  const y = sparkRefY(buf, ref);
  if (y === null) {
    setAttr(refLine, "y1", null);
    setAttr(refLine, "y2", null);
    refLine.style.display = "none";
  } else {
    refLine.style.display = "";
    setAttr(refLine, "y1", y);
    setAttr(refLine, "y2", y);
  }

  const cls = "spark" + (statusClass ? " is-" + statusClass : "");
  if (svg.getAttribute("class") !== cls) svg.setAttribute("class", cls);
}

// ── Formatting ──────────────────────────────────────────────────────────────

export const fmt = (v) => (typeof v === "number" ? v.toFixed(3) : v);

export const fmtNum = (v, digits = 1) =>
  typeof v === "number" ? v.toFixed(digits) : "—";

export function fmtUptime(ms) {
  if (ms == null) return "—";
  const s = Math.floor(ms / 1000);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  if (h) return `${h}h${String(m).padStart(2, "0")}`;
  if (m) return `${m}m${String(sec).padStart(2, "0")}`;
  return `${sec}s`;
}

export const takeDuration = (t) =>
  t.last_ts_rx_us > t.first_ts_rx_us
    ? ((t.last_ts_rx_us - t.first_ts_rx_us) / 1e6).toFixed(1)
    : "0.0";

/** 1234567 → "1 234 567" (thin spaces, French convention). */
export function fmtCount(n) {
  if (n == null) return "—";
  return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, " ");
}
