const DM = (() => {
  const KEY = "documud_token";
  const token = () => localStorage.getItem(KEY);

  function errorText(body, status) {
    const d = body && body.detail;
    if (typeof d === "string") return d;
    if (Array.isArray(d)) return d.map(e => e.msg).join("; ");
    return `Request failed (${status})`;
  }

  async function request(path, opts = {}) {
    const headers = { ...(opts.headers || {}) };
    if (token()) headers.Authorization = `Bearer ${token()}`;
    let res;
    try { res = await fetch(path, { ...opts, headers }); }
    catch { throw new Error("Network error. Check your connection and try again."); }
    if (res.status === 401 && !opts.noRedirect) {
      localStorage.removeItem(KEY);
      location.href = "/";
      throw new Error("Session expired.");
    }
    return res;
  }

  async function api(path, opts = {}) {
    const res = await request(path, opts);
    const body = await res.json().catch(() => null);
    if (!res.ok) throw new Error(errorText(body, res.status));
    return body;
  }

  async function blobUrl(path) {
    const res = await request(path);
    if (!res.ok) throw new Error(errorText(await res.json().catch(() => null), res.status));
    return URL.createObjectURL(await res.blob());
  }

  async function download(path, filename) {
    const url = await blobUrl(path);
    const a = Object.assign(document.createElement("a"), { href: url, download: filename });
    document.body.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
  }

  function el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
      else if (v !== false && v != null) node.setAttribute(k, v === true ? "" : v);
    }
    node.append(...children.flat().filter(c => c != null));
    return node;
  }

  const badge = s => el("span", { class: `badge ${s}` }, s.replace("_", " "));
  const fmtDate = iso => new Date(iso).toLocaleString([], { dateStyle: "medium", timeStyle: "short" });

  function requireAuth() { if (!token()) location.href = "/"; }
  function setToken(t) { localStorage.setItem(KEY, t); }
  function logout() { localStorage.removeItem(KEY); location.href = "/"; }

  function toast(text, kind = "") {
    let box = document.getElementById("toasts");
    if (!box) { box = el("div", { id: "toasts", role: "status", "aria-live": "polite" }); document.body.append(box); }
    const t = el("div", { class: `toast ${kind}` }, text);
    box.append(t);
    setTimeout(() => t.remove(), kind === "error" ? 6000 : 3500);
  }

  document.querySelectorAll("header.top nav a").forEach(a => {
    if (a.getAttribute("href") === location.pathname) a.setAttribute("aria-current", "page");
  });

  const out = document.getElementById("logout");
  if (out) { requireAuth(); out.addEventListener("click", logout); }

  return { api, request, blobUrl, download, el, badge, fmtDate, setToken, token, errorText, toast };
})();
