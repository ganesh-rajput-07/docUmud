const $ = id => document.getElementById(id);
const { api, el, toast, request, errorText } = DM;
const SKIP = new Set(["signature", "initials", "date_signed"]);
let templates = [], tpl = null, hasProfile = false, datasets = [];
const inputs = {};   // field name -> element

async function call(path, body) {
  const res = await request(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const data = await res.json().catch(() => null);
  if (!res.ok) { const e = new Error(errorText(data, res.status)); e.errors = data?.detail?.errors || {}; throw e; }
  return data;
}

function signerRoles() { const used = new Set(tpl.fields.map(f => f.role)); return tpl.roles.filter(r => r.kind === "signer" && used.has(r.id)); }
function hasOffice() { const used = new Set(tpl.fields.map(f => f.role)); return tpl.roles.some(r => r.kind === "office" && used.has(r.id)); }

function renderRecipients() {
  const box = $("recipients"); box.replaceChildren();
  if (hasOffice()) box.append(el("p", { class: "muted" }, "The office role is you. You'll be asked to sign after the data is set, or it signs automatically if you turn that on."));
  signerRoles().forEach(r => { const rb = el("div", { class: "rec-box", "data-role": r.id },
    el("h3", {}, r.label),
    el("div", { class: "grid2" }, el("div", {}, el("label", {}, "Name"), el("input", { type: "text", "data-k": "name", maxlength: 120 })), el("div", {}, el("label", {}, "Email"), el("input", { type: "email", "data-k": "email" }), el("div", { class: "ferr", "data-err": `recipients.${r.id}.email` }))),
    el("div", { class: "grid2" },
      el("div", {}, el("label", {}, "How do they get the link?"), el("select", { "data-k": "deliver" }, el("option", { value: "email" }, "Email it to them"), el("option", { value: "none" }, "Don't email (I'll embed the link)"))),
      el("label", { class: "check-row", style: "margin-top:26px" }, el("input", { type: "checkbox", "data-k": "verify", checked: true }), el("span", {}, "Require an email code"))),
    el("div", { class: "ferr", "data-err": `recipients.${r.id}` }));
    // an embedded recipient has been authenticated by your own app, so don't demand an email code by default
    rb.querySelector("[data-k=deliver]").addEventListener("change", e => { rb.querySelector("[data-k=verify]").checked = e.target.value === "email"; });
    box.append(rb); });
  box.append(el("div", { id: "ccs" }));
}
$("add-cc").addEventListener("click", () => $("ccs").append(el("div", { class: "row cc", style: "margin-top:8px" },
  el("input", { type: "text", placeholder: "Name", "data-k": "name", style: "max-width:200px" }), el("input", { type: "email", placeholder: "cc@example.com", "data-k": "email", style: "max-width:260px" }),
  el("button", { class: "sm", type: "button", onclick: e => e.target.closest(".cc").remove() }, "Remove"))));

function dataInput(f, groupFields) {
  const opts = f.type === "radio" ? groupFields.map(g => g.option) : f.options;
  let i;
  if (["description", "address"].includes(f.type)) i = el("textarea", { rows: 3 });
  else if (f.type === "dropdown" || f.type === "radio") i = el("select", {}, el("option", { value: "" }, "— choose —"), ...opts.map(o => el("option", { value: o }, o)));
  else if (f.type === "checkbox") i = el("select", {}, el("option", { value: "" }, "— leave for the signer —"), el("option", { value: "true" }, "Ticked"), el("option", { value: "false" }, "Not ticked"));
  else i = el("input", { type: { date: "date", email: "email", phone: "tel" }[f.type] || "text", inputmode: ["amount", "number", "percentage"].includes(f.type) ? "decimal" : null });
  if (f.default_value && !["checkbox"].includes(f.type)) i.value = f.default_value;
  return i;
}
function renderData() {
  const box = $("data"); box.replaceChildren(); Object.keys(inputs).forEach(k => delete inputs[k]);
  tpl.roles.forEach(role => {
    const seen = new Set(), fields = tpl.fields.filter(f => f.role === role.id && !SKIP.has(f.type) && !seen.has(f.name) && seen.add(f.name));
    if (!fields.length) return;
    box.append(el("div", { class: "fgroup" }, el("h3", {}, role.kind === "office" ? `${role.label} (you) fills` : `${role.label} fills`),
      el("p", { class: "muted small" }, role.kind === "office" ? "These are printed on the document for you." : "Leave blank and the signer fills these in; anything you enter is locked."),
      ...fields.map(f => { const group = tpl.fields.filter(g => g.name === f.name), i = dataInput(f, group); inputs[f.name] = i;
        return el("div", {}, el("label", {}, (f.label || DM.humanize(f.name)) + (f.required && role.kind === "office" ? " *" : "")), i, el("div", { class: "ferr", "data-err": `data.${f.name}` })); })));
  });
  if (!box.children.length) box.append(el("p", { class: "muted" }, "This template has no data fields. Only signatures are collected."));
}
function renderOptions() {
  $("o-name").value = tpl.name; $("order-row").hidden = signerRoles().length < 2;
  $("auto-row").hidden = !hasOffice(); $("o-auto").disabled = !hasProfile; $("o-auto").checked = hasProfile && hasOffice();
  $("auto-note").replaceChildren(hasProfile ? "Uses the signature saved in Templates → Office signature." : el("span", {}, "No office signature saved yet. ", el("a", { href: "/templates" }, "Save one")));
  $("tpl-note").replaceChildren(`${tpl.fields.length} fields · roles: ${tpl.roles.map(r => r.label).join(", ")} · `, el("a", { href: `/templates/${tpl.id}/design` }, "Edit template"));
}
function gather() {
  const recipients = [...document.querySelectorAll(".rec-box")].map(b => ({ role: b.dataset.role, name: b.querySelector('[data-k=name]').value.trim(), email: b.querySelector('[data-k=email]').value.trim(),
    deliver: b.querySelector('[data-k=deliver]').value, verify: b.querySelector('[data-k=verify]').checked }));
  document.querySelectorAll(".cc").forEach(c => { const email = c.querySelector('[data-k=email]').value.trim(); if (email) recipients.push({ role: "cc", kind: "cc", name: c.querySelector('[data-k=name]').value.trim(), email }); });
  const data = {}; Object.entries(inputs).forEach(([k, i]) => { if (i.value !== "") data[k] = i.value; });
  return { recipients, data };
}
function showErrors(errors) {
  document.querySelectorAll(".ferr").forEach(e => { e.textContent = ""; }); document.querySelectorAll(".bad").forEach(e => e.classList.remove("bad"));
  let first = null;
  Object.entries(errors).forEach(([k, v]) => { const t = document.querySelector(`[data-err="${k}"]`); if (t) { t.textContent = v; first ||= t; const name = k.replace(/^data\./, ""); inputs[name]?.classList.add("bad"); } });
  first?.scrollIntoView({ block: "center", behavior: "smooth" });
}
function common() {
  return { message: $("o-msg").value, signing_order: $("o-order").value, expires_in_days: +$("o-days").value, office_auto_sign: $("o-auto").checked && !$("o-auto").disabled, send: !$("o-draft").checked };
}
$("create").addEventListener("click", async () => {
  const m = $("msg"); m.className = "msg"; m.textContent = ""; showErrors({}); $("create").disabled = true;
  try {
    const { recipients, data } = gather();
    const env = await call("/v1/envelopes", { template_id: tpl.id, name: $("o-name").value.trim() || null, external_id: $("o-ref").value.trim() || null, recipients, data, ...common() });
    location.href = `/envelopes/${env.id}`;
  } catch (e) { m.className = "msg error"; m.textContent = Object.keys(e.errors || {}).length ? "Please fix the highlighted fields." : e.message; showErrors(e.errors || {}); $("create").disabled = false; }
});

// ---------------- many at once ----------------
function renderBatchMap() {
  const ds = datasets.find(d => d.id === $("b-ds").value), box = $("b-map"); box.replaceChildren(); $("b-go").disabled = !ds;
  if (!ds) return;
  const guess = re => ds.columns.find(c => re.test(c)) || "";
  signerRoles().forEach(r => box.append(el("div", { class: "rec-box", "data-brole": r.id }, el("h3", {}, r.label),
    el("label", {}, "Name column"), el("select", { "data-k": "name" }, el("option", { value: "" }, "(none)"), ...ds.columns.map(c => el("option", { value: c, selected: c === guess(/name/i) }, c))),
    el("label", {}, "Email column"), el("select", { "data-k": "email" }, ...ds.columns.map(c => el("option", { value: c, selected: c === guess(/e-?mail/i) }, c))))));
  $("b-go").textContent = `Create ${ds.row_count} envelope${ds.row_count === 1 ? "" : "s"}`;
}
$("b-ds").addEventListener("change", renderBatchMap);
$("b-go").addEventListener("click", async () => {
  const ds = datasets.find(d => d.id === $("b-ds").value), m = $("b-msg"); if (!ds) return;
  if (!confirm(`Create ${ds.row_count} envelopes${$("o-draft").checked ? " as drafts" : " and send them"}?`)) return;
  $("b-go").disabled = true; m.className = "msg"; m.textContent = "Reading the sheet…";
  try {
    const rows = []; for (let off = 0; off < ds.row_count; off += 500) rows.push(...(await api(`/datasets/${ds.id}?offset=${off}&limit=500`)).rows);
    const maps = [...document.querySelectorAll("[data-brole]")].map(b => ({ role: b.dataset.brole, name: b.querySelector("[data-k=name]").value, email: b.querySelector("[data-k=email]").value }));
    const items = rows.map(r => { const row = Object.fromEntries(ds.columns.map((c, i) => [c, r[i]]));
      return { data: row, recipients: maps.map(x => ({ role: x.role, name: x.name ? row[x.name] : "", email: row[x.email], deliver: "email" })) }; });
    m.textContent = "Submitting…";
    const b = await call("/v1/envelopes/batch", { template_id: tpl.id, items, ...common() });
    pollBatch(b.id);
  } catch (e) { m.className = "msg error"; m.textContent = e.message; $("b-go").disabled = false; }
});
async function pollBatch(id) {
  const m = $("b-msg");
  const b = await api(`/v1/batches/${id}`);
  m.className = "msg"; m.replaceChildren(`${b.created + b.failed} of ${b.total} processed · ${b.created} created${b.failed ? `, ${b.failed} failed` : ""}`);
  if (b.status !== "completed") return setTimeout(() => pollBatch(id).catch(() => {}), 2000);
  m.className = `msg ${b.failed ? "error" : "ok"}`;
  m.replaceChildren(`Done: ${b.created} created${b.failed ? `, ${b.failed} failed.` : "."} `, el("a", { href: "/envelopes" }, "View envelopes"),
    b.failed ? el("ul", { class: "muted" }, b.errors.slice(0, 5).map(e => el("li", {}, `Row ${e.index + 1}: ${e.error}${e.errors ? " (" + Object.values(e.errors).join("; ") + ")" : ""}`))) : null);
}

// ---------------- boot ----------------
async function pick(id) {
  tpl = await api(`/v1/templates/${id}`);
  renderRecipients(); renderData(); renderOptions(); renderBatchMap();
}
$("tpl").addEventListener("change", e => pick(e.target.value).catch(x => toast(x.message, "error")));
(async () => {
  try {
    [{ data: templates }, datasets, { has_signature: hasProfile }] = await Promise.all([api("/v1/templates"), api("/datasets"), api("/v1/signature-profile")]);
    if (!templates.length) { $("tpl-note").textContent = "You have no templates yet."; $("tpl-note").append(" ", el("a", { href: "/templates" }, "Create one")); $("create").disabled = true; return; }
    const want = new URLSearchParams(location.search).get("template");
    $("tpl").replaceChildren(...templates.map(t => el("option", { value: t.id, selected: t.id === want }, `${t.name} (${t.field_count} fields)`)));
    $("b-ds").replaceChildren(el("option", { value: "" }, "Choose a sheet…"), ...datasets.map(d => el("option", { value: d.id }, `${d.name} · ${d.row_count} rows`)));
    await pick($("tpl").value);
  } catch (e) { toast(e.message, "error"); }
})();
