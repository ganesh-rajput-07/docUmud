// Recipient signing page: the document with this person's fields placed on it by the office.
const $ = id => document.getElementById(id);
const { el } = DM;
const token = window.SIGN_TOKEN, api = p => `/v1/sign/${token}${p}`;
let verifyToken = null, info = null;
const vq = () => (verifyToken ? `?v=${encodeURIComponent(verifyToken)}` : "");
const vh = () => (verifyToken ? { "X-Verify": verifyToken } : {});

async function call(path, opts = {}) {
  const res = await fetch(api(path), { ...opts, headers: { ...vh(), ...(opts.headers || {}) } });
  const data = await res.json().catch(() => null);
  if (!res.ok) throw Object.assign(new Error(DM.errorText(data, res.status)), { status: res.status, errors: data?.detail?.errors || {} });
  return data;
}
function showState(title, text, actions = []) {
  ["otp-card", "doc"].forEach(i => { $(i).hidden = true; });
  $("state").hidden = false; $("state-title").textContent = title; $("state-text").textContent = text; $("state-actions").replaceChildren(...actions);
}

// ---------------- email code ----------------
const otpMsg = (t, cls = "") => { $("otp-msg").className = `msg ${cls}`; $("otp-msg").textContent = t; };
$("otp-send").addEventListener("click", async () => {
  $("otp-send").disabled = true; otpMsg("Sending…");
  try { const r = await call("/otp/send", { method: "POST" }); $("otp-entry").hidden = false; $("otp-code").focus(); otpMsg(`Code sent to ${r.email_hint}. It expires in ${r.expires_minutes} minutes.`, "ok"); $("otp-send").textContent = "Send a new code"; setTimeout(() => { $("otp-send").disabled = false; }, 30000); }
  catch (e) { otpMsg(e.message, "error"); $("otp-send").disabled = false; }
});
$("otp-verify").addEventListener("click", async () => {
  const code = $("otp-code").value.replace(/\D/g, ""); if (code.length !== 6) return otpMsg("Enter the 6-digit code from your email.", "error");
  $("otp-verify").disabled = true;
  try { verifyToken = (await call("/otp/verify", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ code }) })).verify_token; await start(); }
  catch (e) { otpMsg(e.message, "error"); }
  $("otp-verify").disabled = false;
});
$("otp-code").addEventListener("keydown", e => { if (e.key === "Enter") $("otp-verify").click(); });

// ---------------- document ----------------
const values = {}, units = [], sigs = { signature: null, initials: null }, sigUrls = {};
const MULTI = new Set(["description", "address"]);

function unitDone(u) { return !u.required || u.done(); }
function refresh() {
  const req = units.filter(u => u.required), n = req.filter(u => u.done()).length;
  $("progress").textContent = req.length ? `${n} of ${req.length} required fields done` : "Nothing required: review and finish";
  $("progress-bar").style.width = `${req.length ? 100 * n / req.length : 100}%`; $("finish").disabled = n < req.length;
  units.forEach(u => { u.boxes.forEach(b => { b.classList.toggle("ok", u.done()); b.classList.toggle("opt", !u.required && !u.done()); if (u.done()) b.classList.remove("err"); }); });
}

function buildField(f) {
  const box = el("div", { class: "sp-field", style: `left:${f.x * 100}%;top:${f.y * 100}%;width:${f.w * 100}%;height:${f.h * 100}%`, title: f.label || f.name });
  let unit = units.find(u => u.name === f.name && f.type === "radio");
  const track = (done, extra = {}) => { if (!unit) { unit = { name: f.name, required: f.required, boxes: [], done, ...extra }; units.push(unit); } unit.boxes.push(box); };
  const t = f.type;
  if (t === "signature" || t === "initials") {
    const b = el("button", { type: "button", class: "sp-sign", "aria-label": t === "signature" ? "Sign here" : "Add initials" }, t === "signature" ? "Sign here" : "Initial");
    b.addEventListener("click", async () => {
      const blob = await SigPad.open({ title: t === "signature" ? "Draw, type or upload your signature" : "Your initials", text: t === "initials" ? initials() : $("env-from").dataset.name || "" });
      if (!blob) return; sigs[t] = blob; if (sigUrls[t]) URL.revokeObjectURL(sigUrls[t]); sigUrls[t] = URL.createObjectURL(blob);
      document.querySelectorAll(`.sp-sign[data-t="${t}"]`).forEach(x => x.replaceChildren(el("img", { src: sigUrls[t], alt: "Your signature" }))); refresh();
    });
    b.dataset.t = t; box.append(b); track(() => !!sigs[t], { kind: "sign" }); return box;
  }
  if (t === "checkbox") {
    const i = el("input", { type: "checkbox", "aria-label": f.label || f.name }); i.checked = f.value === "true"; values[f.name] = i.checked ? "true" : "false";
    i.addEventListener("change", () => { values[f.name] = i.checked ? "true" : "false"; refresh(); }); box.append(el("label", { class: "sp-check" }, i)); track(() => i.checked); return box;
  }
  if (t === "radio") {
    const i = el("input", { type: "radio", name: `r-${f.name}`, value: f.option, "aria-label": f.option }); i.checked = f.value === f.option; if (i.checked) values[f.name] = f.option;
    i.addEventListener("change", () => { values[f.name] = f.option; refresh(); }); box.append(el("label", { class: "sp-check" }, i));
    track(() => !!values[f.name]); if (f.required) unit.required = true; return box;
  }
  let input;
  if (t === "dropdown") input = el("select", { "aria-label": f.label || f.name }, el("option", { value: "" }, "Select…"), ...f.options.map(o => el("option", { value: o }, o)));
  else if (MULTI.has(t)) input = el("textarea", { "aria-label": f.label || f.name, placeholder: f.label || "" });
  else input = el("input", { type: { date: "date", email: "email", phone: "tel" }[t] || "text", "aria-label": f.label || f.name, placeholder: f.label || DM.humanize(f.name), inputmode: ["amount", "number", "percentage"].includes(t) ? "decimal" : null });
  input.value = f.value || ""; values[f.name] = input.value; if (f.style?.align) input.style.textAlign = f.style.align;
  input.addEventListener("input", () => { values[f.name] = input.value; refresh(); }); box.append(input);
  track(() => (values[f.name] || "").trim() !== "", { input }); return box;
}
const initials = () => ($("env-from").dataset.name || "").split(/\s+/).filter(Boolean).map(w => w[0].toUpperCase()).join("").slice(0, 3);

function buildDocument() {
  const pages = $("pages"); pages.replaceChildren(); units.length = 0;
  const byPage = {}; info.fields.forEach(f => (byPage[f.page] ||= []).push(f));
  const cleanQ = vq();
  for (let p = 1; p <= info.pages; p++) {
    const img = el("img", { alt: `Page ${p}`, loading: p > 2 ? "lazy" : "eager", src: api(`/pages/${p}.png`) + cleanQ });
    const wrap = el("div", { class: "sp-page" }, img);
    (byPage[p] || []).forEach(f => wrap.append(buildField(f)));
    new ResizeObserver(([e]) => wrap.style.setProperty("--fs", `${Math.max(9, e.contentRect.width * 0.0185)}px`)).observe(wrap);
    pages.append(wrap);
  }
  refresh();
}

function nextField() {
  const u = units.find(x => x.required && !x.done());
  if (!u) { $("finish").scrollIntoView({ block: "center", behavior: "smooth" }); $("finish").focus(); return; }
  const b = u.boxes[0]; b.scrollIntoView({ block: "center", behavior: "smooth" }); b.classList.add("pulse"); setTimeout(() => b.classList.remove("pulse"), 2200);
  setTimeout(() => (u.input || b.querySelector("input,select,textarea,button"))?.focus({ preventScroll: true }), 350);
}
$("next-field").addEventListener("click", nextField);

// ---------------- finish / decline ----------------
$("finish").addEventListener("click", () => {
  const filled = units.filter(u => u.done()).length;
  $("finish-summary").textContent = `${filled} field${filled === 1 ? "" : "s"} completed. Once you finish, you can't change them.`; $("finish-msg").textContent = ""; $("consent").checked = false; $("finish-dialog").showModal();
});
$("finish-cancel").addEventListener("click", () => $("finish-dialog").close());
$("finish-go").addEventListener("click", async () => {
  const m = $("finish-msg"); m.className = "msg";
  if (!$("consent").checked) { m.className = "msg error"; m.textContent = "Please tick the box to agree to sign electronically."; return; }
  const fd = new FormData(); fd.append("values", JSON.stringify(values)); fd.append("consent", "true");
  if (sigs.signature) fd.append("signature", sigs.signature, "signature.png"); if (sigs.initials) fd.append("initials", sigs.initials, "initials.png");
  $("finish-go").disabled = true; m.textContent = "Signing…";
  try {
    const r = await call("/submit", { method: "POST", body: fd }); $("finish-dialog").close(); done(r);
  } catch (e) {
    m.className = "msg error"; m.textContent = e.message;
    const bad = units.filter(u => e.errors[u.name] || (u.kind === "sign" && (e.errors.signature || e.errors.initials)));
    if (bad.length) { bad.forEach(u => u.boxes.forEach(b => b.classList.add("err"))); $("finish-dialog").close(); bad[0].boxes[0].scrollIntoView({ block: "center", behavior: "smooth" }); DM.toast(`${bad.length} field${bad.length > 1 ? "s need" : " needs"} attention`, "error"); }
    $("finish-go").disabled = false;
  }
});
function done(r) {
  const actions = [];
  if (r.completed) actions.push(el("a", { class: "btn primary", href: api("/document") + vq() }, "Download signed copy"));
  if (r.return_url) actions.push(el("a", { class: r.completed ? "btn" : "btn primary", href: r.return_url }, "Continue"));
  showState("Signed ✓", r.completed ? "Everyone has signed. The completed document is ready." : "Thank you. You'll get the completed copy by email once everyone has signed.", actions);
}
$("decline").addEventListener("click", async () => {
  const reason = prompt("Decline to sign? Tell the sender why (optional):"); if (reason === null) return;
  try { await call("/decline", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ reason }) }); showState("You declined to sign", "The sender has been told. You can close this page."); }
  catch (e) { DM.toast(e.message, "error"); }
});

// ---------------- boot ----------------
async function start() {
  info = await call("");
  if (info.otp_required && !info.verified) {
    $("state").hidden = true; $("doc").hidden = true; $("otp-card").hidden = false;
    $("otp-text").textContent = `To protect this document, we'll email a 6-digit code to ${info.email_hint}.`; return;
  }
  $("otp-card").hidden = true;
  const e = info.envelope, r = info.recipient, ret = info.return_url ? [el("a", { class: "btn", href: info.return_url }, "Continue")] : [];
  document.title = `${e.name} | Sign`;
  if (!info.can_sign) {
    const dl = e.status === "completed" ? [el("a", { class: "btn primary", href: api("/document") + vq() }, "Download signed copy")] : [];
    if (e.status === "completed") return showState("All done ✓", "This document has been signed by everyone.", [...dl, ...ret]);
    if (e.status === "declined") return showState("This envelope was declined", "It can no longer be signed.");
    if (e.status === "cancelled") return showState("This request was cancelled", `${info.sender} withdrew it. Contact them if you think this is a mistake.`);
    if (e.status === "expired") return showState("This link has expired", `Ask ${info.sender} to send it again.`);
    if (r.status === "signed") return showState("You've already signed", "We'll email the completed copy when everyone has signed.", ret);
    if (r.status === "waiting") return showState("It's not your turn yet", `Waiting for ${(info.waiting_for || []).join(", ") || "someone else"} to sign first. We'll email you when it's ready.`);
    return showState("Nothing to sign", "This request isn't available.");
  }
  $("state").hidden = true; $("doc").hidden = false;
  $("env-name").textContent = e.name; $("env-from").textContent = `From ${info.sender}${r.name ? ` · for ${r.name}` : ""}`; $("env-from").dataset.name = r.name || "";
  $("env-msg").hidden = !e.message; $("env-msg").textContent = e.message || "";
  buildDocument();
}
start().catch(e => showState(e.status === 404 ? "Link not found" : "Something went wrong", e.status === 404 ? "This signing link isn't valid. Check you copied the whole link." : e.message));
