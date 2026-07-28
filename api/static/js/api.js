// ── HTTP helper + toasts ────────────────────────────────────────────────────
// Commands are REST; observation is pushed over the WS (see store.js).

export async function api(method, path, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  let data = null;
  try { data = await res.json(); } catch { /* no body */ }
  if (!res.ok) {
    throw new Error((data && data.detail) || res.statusText);
  }
  return data;
}

const TOAST_MS = 4000;

export function toast(msg, kind = "ok") {
  const host = document.getElementById("toasts");
  if (!host) return;

  const el = document.createElement("div");
  el.className = "toast " + kind;
  el.textContent = msg;
  host.append(el);

  setTimeout(() => {
    el.classList.add("is-leaving");
    el.addEventListener("animationend", () => el.remove(), { once: true });
    // Fallback if animations are disabled (prefers-reduced-motion).
    setTimeout(() => el.remove(), 400);
  }, TOAST_MS);
}

// Wrap an async command: report success/failure in a toast.
export function action(fn, okMsg = "OK") {
  return async (...args) => {
    try {
      const r = await fn(...args);
      toast(okMsg, "ok");
      return r;
    } catch (e) {
      toast(e.message, "bad");
    }
  };
}
