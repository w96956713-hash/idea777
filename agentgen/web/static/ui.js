// Small UI toolkit: element builder, API client, formatting and shared components.

export class ApiError extends Error {
  constructor(message, status) { super(message); this.status = status; }
}

let onUnauthorized = () => {};
export function setUnauthorizedHandler(fn) { onUnauthorized = fn; }

export async function api(method, path, body) {
  const opts = { method, credentials: "same-origin", headers: { "X-Requested-With": "agentgen" } };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch("/api" + path, opts);
  } catch {
    throw new ApiError("Can't reach the server. Check that it is running.", 0);
  }
  let data = null;
  try { data = await res.json(); } catch { /* empty body */ }
  if (res.status === 401 && !path.startsWith("/auth/")) {
    onUnauthorized();
    throw new ApiError("Please sign in.", 401);
  }
  if (!res.ok) throw new ApiError(errorText(data, res.status), res.status);
  return data;
}

function errorText(data, status) {
  const d = data && data.detail;
  if (typeof d === "string") return d;
  if (Array.isArray(d)) {
    return d.map((e) => {
      const field = (e.loc || []).filter((x) => x !== "body").join(".");
      return field ? `${field}: ${e.msg}` : e.msg;
    }).join("; ");
  }
  return `Request failed (${status}).`;
}

// h("div", {class: "x", onclick: fn, text: "..."}, child, [children], null)
export function h(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k === "value") el.value = v;
    else if (k === "checked" || k === "disabled" || k === "selected" || k === "open") el[k] = !!v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  append(el, children);
  return el;
}

// Like el.replaceChildren, but skips null/false (replaceChildren would insert the text "null").
export function setChildren(el, ...children) {
  el.replaceChildren();
  append(el, children);
}

function append(el, children) {
  for (const c of children) {
    if (c === null || c === undefined || c === false) continue;
    if (Array.isArray(c)) append(el, c);
    else el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
}

// ---------------------------------------------------------------- formatting

export const fmt = {
  date(s) {
    if (!s) return "—";
    const d = new Date(s.endsWith("Z") || s.includes("+") ? s : s + "Z");
    return d.toLocaleString(undefined, { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  },
  ago(s) {
    if (!s) return "—";
    const d = new Date(s.endsWith("Z") || s.includes("+") ? s : s + "Z");
    const sec = Math.round((Date.now() - d.getTime()) / 1000);
    if (sec < 60) return "just now";
    if (sec < 3600) return `${Math.floor(sec / 60)} min ago`;
    if (sec < 86400) return `${Math.floor(sec / 3600)} h ago`;
    return fmt.date(s);
  },
  usd(v) {
    if (v === null || v === undefined) return "—";
    if (v === 0) return "$0";
    return v < 0.01 ? `$${v.toFixed(4)}` : `$${v.toFixed(2)}`;
  },
  ms(v) {
    if (v === null || v === undefined) return "—";
    return v < 1000 ? `${Math.round(v)} ms` : `${(v / 1000).toFixed(1)} s`;
  },
  tokens(v) {
    if (v === null || v === undefined) return "—";
    return v >= 1000 ? `${(v / 1000).toFixed(1)}k` : String(v);
  },
  pct(v) { return v === null || v === undefined ? "—" : `${Math.round(v * 100)}%`; },
  json(v) { return JSON.stringify(v, null, 2); },
};

// ---------------------------------------------------------------- components

export function pageHead(title, sub, actions = [], crumb = null) {
  return h("div", { class: "page-head" },
    h("div", {}, crumb ? h("div", { class: "crumb" }, crumb) : null, h("h1", { text: title }),
      sub ? h("p", { class: "sub", text: sub }) : null),
    h("div", { class: "row" }, actions));
}

export function card(title, hint, children = [], actions = []) {
  return h("section", { class: "card" },
    title || actions.length ? h("div", { class: "card-head" },
      h("div", {}, title ? (title instanceof Node ? title : h("h2", { text: title })) : null,
        hint ? h("p", { class: "hint", text: hint }) : null),
      h("div", { class: "row" }, actions)) : null,
    children);
}

export function badge(text, kind = "") { return h("span", { class: `badge ${kind}`, text }); }

const STATUS_KIND = {
  passed: "ok", completed: "ok", succeeded: "ok", done: "ok", approved: "ok", deployed: "ok", supported: "ok",
  available: "ok", designed: "ok",
  failed: "bad", denied: "bad", error: "bad", needs_custom_connector: "bad", needs_connector: "bad", cancelled: "",
  draft: "warn", pending: "warn", queued: "warn", running: "info", retrying: "warn", needs_customer: "warn",
  via_http_api: "info", data_import: "info", unknown: "", analysed: "info", discovery: "info", intake: "",
};
export function statusBadge(status) { return badge(String(status ?? "—").replaceAll("_", " "), STATUS_KIND[status] ?? ""); }

export function notice(kind, ...content) { return h("div", { class: `notice ${kind}` }, content); }

export function table(cols, rows, { empty = "Nothing here yet.", onRow = null } = {}) {
  if (!rows.length) return h("div", { class: "table-wrap" }, h("p", { class: "empty", text: empty }));
  return h("div", { class: "table-wrap" }, h("table", {},
    h("thead", {}, h("tr", {}, cols.map((c) => h("th", { class: c.class || "", text: c.label })))),
    h("tbody", {}, rows.map((r) => h("tr", {
      class: onRow ? "clickable" : "",
      onclick: onRow ? (e) => { if (!e.target.closest("a,button,input,select")) onRow(r); } : null,
    }, cols.map((c) => {
      const v = c.render ? c.render(r) : r[c.key];
      return h("td", { class: c.class || "" }, v === null || v === undefined || v === "" ? "—" : v);
    }))))));
}

export function field(label, input, help) {
  return h("label", { class: "field" }, h("span", {}, label, help ? h("span", { class: "help", text: ` — ${help}` }) : null), input);
}

export function kv(pairs) {
  return h("dl", { class: "kv" }, pairs.filter(Boolean).flatMap(([k, v]) => [h("dt", { text: k }), h("dd", {}, v ?? "—")]));
}

export function list(items, empty = "None.") {
  if (!items || !items.length) return h("p", { class: "muted", text: empty });
  return h("ul", { class: "plain" }, items.map((i) => h("li", {}, i)));
}

export function stat(label, value, alert = false) {
  return h("div", { class: `stat${alert ? " alert" : ""}` }, h("div", { class: "v" }, value), h("div", { class: "l", text: label }));
}

export function progress(fraction, kind = "") {
  const bar = h("div");
  bar.style.width = `${Math.max(0, Math.min(1, fraction || 0)) * 100}%`;
  return h("div", { class: `progress ${kind}` }, bar);
}

let toastBox = null;
export function toast(message, kind = "") {
  if (!toastBox) { toastBox = h("div", { class: "toasts", role: "status", "aria-live": "polite" }); document.body.append(toastBox); }
  const t = h("div", { class: `toast ${kind}`, text: message });
  toastBox.append(t);
  setTimeout(() => t.remove(), kind === "bad" ? 7000 : 4000);
}

// A button that runs an async action, disables itself meanwhile and reports errors.
export function actionButton(label, fn, { kind = "", confirmText = null, small = false, disabled = false, title = null } = {}) {
  let armed = false;
  const btn = h("button", { class: `${kind}${small ? " small" : ""}`, type: "button", disabled, title }, label);
  btn.addEventListener("click", async () => {
    if (confirmText && !armed) {
      armed = true;
      btn.textContent = confirmText;
      setTimeout(() => { armed = false; btn.textContent = label; }, 4000);
      return;
    }
    btn.disabled = true;
    try { await fn(); } catch (e) { toast(e.message, "bad"); } finally { btn.disabled = false; armed = false; if (btn.isConnected) btn.textContent = label; }
  });
  return btn;
}

// Poll a background operation until it finishes. Returns the final operation.
export async function waitForOperation(op, onUpdate, isCurrent = () => true) {
  let current = op;
  while (current.status === "queued" || current.status === "running") {
    onUpdate?.(current);
    await new Promise((r) => setTimeout(r, 1500));
    if (!isCurrent()) return current;
    current = await api("GET", `/operations/${current.id}`);
  }
  onUpdate?.(current);
  return current;
}

export function operationView(op, label) {
  if (!op) return null;
  if (op.status === "queued" || op.status === "running") {
    const started = new Date(op.created_at + (op.created_at.includes("+") ? "" : "Z"));
    const secs = Math.max(0, Math.round((Date.now() - started.getTime()) / 1000));
    const prog = op.result && op.result.done !== undefined ? ` · ${op.result.done} done` : "";
    return h("div", { class: "op" }, h("span", { class: "spinner", "aria-hidden": "true" }),
      `${label}… ${secs}s${prog}. Model calls can take a minute or two.`);
  }
  if (op.status === "failed") {
    const retry = op.result && op.result.retryable
      ? h("span", { class: "small", text: "This is usually temporary - try again in a minute." }) : null;
    return notice("bad", h("b", { text: `${label} failed` }), op.error || "Unknown error", retry);
  }
  return null;
}
