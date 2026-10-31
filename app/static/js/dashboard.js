const $ = id => document.getElementById(id);
const { el, badge, fmtDate, api, toast } = DM;
const state = { docs: [], reqs: [], jobs: [] };
const SERVICE = { ocr: "OCR", pdf_tagging: "Accessibility tagging" };
const SOURCE = { batch: "Batch copy", uploaded: "Uploaded", scanned: "Scanned", signed: "Signed", tagged: "Accessible", ocr: "OCR", annotated: "Annotated", filled: "Filled" };

// Skip re-rendering when polled data is unchanged, and never rebuild the table under an open menu.
const seen = {};
const changed = (key, data) => { const j = JSON.stringify(data); if (seen[key] === j) return false; seen[key] = j; return true; };
const menuOpen = () => !!document.querySelector("details.menu[open]");
let docsDeferred = false;

const fail = e => toast(e.message, "error");
const note = (target, text, cls = "") => { target.className = `msg ${cls}`; target.textContent = text; };
const btn = (label, fn, cls = "") => el("button", { class: `sm ${cls}`.trim(), onclick: fn }, label);

function table(id, head, rows, empty) {
  const box = $(id); box.replaceChildren();
  if (!rows.length) return box.append(el("div", { class: "empty" }, el("b", {}, empty[0]), empty[1]));
  box.append(el("div", { class: "table-wrap" }, el("table", {},
    el("thead", {}, el("tr", {}, head.map(h => el("th", { class: h === "" ? "end" : "" }, h)))),
    el("tbody", {}, rows))));
}

// ---------- tabs ----------
function showTab(name) {
  document.querySelectorAll(".tabs-bar button").forEach(b => b.setAttribute("aria-selected", String(b.dataset.tab === name)));
  document.querySelectorAll("[role=tabpanel]").forEach(p => { p.hidden = p.id !== `tab-${name}`; });
  history.replaceState(null, "", `#${name}`);
}
document.querySelectorAll(".tabs-bar button").forEach(b => b.addEventListener("click", () => showTab(b.dataset.tab)));
showTab(["documents", "signatures", "data", "processing", "developers"].includes(location.hash.slice(1)) ? location.hash.slice(1) : "documents");
$("base-url").textContent = location.origin;

// ---------- row action menu ----------
function menu(items) {
  const list = el("div", { class: "menu-list" }, items);
  const d = el("details", { class: "menu" }, el("summary", { class: "btn sm" }, "More ▾"), list);
  d.addEventListener("toggle", () => {
    if (!d.open) { if (docsDeferred && !menuOpen()) renderDocs(); return; }
    document.querySelectorAll("details.menu[open]").forEach(o => { if (o !== d) o.open = false; });
    const r = d.getBoundingClientRect(), h = list.offsetHeight;
    list.style.left = `${Math.max(8, Math.min(r.right - list.offsetWidth, innerWidth - list.offsetWidth - 8))}px`;
    list.style.top = `${r.bottom + h + 8 > innerHeight ? Math.max(8, r.top - h - 4) : r.bottom + 4}px`;
  });
  list.addEventListener("click", () => { d.open = false; });
  return d;
}
document.addEventListener("click", e => document.querySelectorAll("details.menu[open]").forEach(d => { if (!d.contains(e.target)) d.open = false; }));
addEventListener("scroll", () => document.querySelectorAll("details.menu[open]").forEach(d => { d.open = false; }), true);
const item = (label, fn) => el("button", { onclick: fn }, label);
const link = (label, href) => el("a", { href }, label);

// ---------- stats ----------
function stats() {
  $("st-docs").textContent = state.docs.length;
  $("st-pending").textContent = state.reqs.filter(r => r.status === "pending").length;
  $("st-signed").textContent = state.reqs.filter(r => r.status === "signed").length;
  $("st-jobs").textContent = state.jobs.filter(j => ["queued", "processing"].includes(j.status)).length;
}

// ---------- documents ----------
async function enqueue(path, label) {
  try { await api(path, { method: "POST" }); toast(`${label} queued. Make sure the worker is running.`, "ok"); await loadJobs(); showTab("processing"); }
  catch (e) { fail(e); }
}
const dl = (id, name) => DM.download(`/documents/${id}/download`, name).catch(fail);

function renderDocs() {
  if (menuOpen()) { docsDeferred = true; return; }
  docsDeferred = false;
  const q = $("doc-filter").value.trim().toLowerCase();
  const docs = state.docs.filter(d => d.filename.toLowerCase().includes(q));
  table("docs", ["Name", "Pages", "Type", "Added", ""], docs.map(d => el("tr", {},
    el("td", { class: "name", title: d.filename }, d.filename), el("td", {}, String(d.pages)),
    el("td", {}, el("span", { class: "badge neutral" }, SOURCE[d.source] || d.source)), el("td", {}, fmtDate(d.created_at)),
    el("td", { class: "actions" },
      btn("Download", () => dl(d.id, d.filename)),
      menu([
        link("Sign it myself", `/documents/${d.id}/sign`),
        item("Request signature…", () => openRequest(d)),
        link("Send in bulk…", `/documents/${d.id}/bulk`),
        el("hr"),
        link("Fill from data", `/documents/${d.id}/fill`),
        link("Tag data", `/documents/${d.id}/tagger`),
        item("Run OCR", () => enqueue(`/documents/${d.id}/ocr`, "OCR")),
        item("View text", () => showText(d)),
        item("Accessibility tagging", () => enqueue(`/documents/${d.id}/tag`, "Accessibility tagging")),
      ])))),
    q ? ["No matches", "Try a different search."] : ["No documents yet", "Upload a PDF or scan a paper document to get started."]);
}
async function loadDocs() { state.docs = await api("/documents"); if (changed("docs", state.docs)) renderDocs(); stats(); }
$("doc-filter").addEventListener("input", renderDocs);

// ---------- upload ----------
const drop = $("dropzone"), fileInput = $("upload-file");
const showPicked = () => { $("drop-name").textContent = fileInput.files[0]?.name || "PDF, JPG or PNG up to 25 MB"; };
fileInput.addEventListener("change", showPicked);
["dragenter", "dragover"].forEach(t => drop.addEventListener(t, e => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach(t => drop.addEventListener(t, e => { e.preventDefault(); drop.classList.remove("over"); }));
drop.addEventListener("drop", e => { if (e.dataTransfer.files.length) { fileInput.files = e.dataTransfer.files; showPicked(); } });

async function upload(openTagger) {
  const f = fileInput.files[0], m = $("upload-msg");
  if (!f) return note(m, "Choose a file first.", "error");
  const form = new FormData(); form.append("file", f);
  const buttons = [$("upload-btn"), $("upload-tag-btn")];
  buttons.forEach(b => { b.disabled = true; }); note(m, "Uploading…");
  try {
    const doc = await api("/documents", { method: "POST", body: form });
    if (openTagger) { location.href = `/documents/${doc.id}/tagger`; return; }
    fileInput.value = ""; showPicked(); note(m, ""); toast("Document uploaded", "ok"); await loadDocs();
  } catch (e) { note(m, e.message, "error"); }
  buttons.forEach(b => { b.disabled = false; });
}
$("upload-btn").addEventListener("click", () => upload(false));
$("upload-tag-btn").addEventListener("click", () => upload(true));

// ---------- signature requests ----------
async function loadRequests() {
  state.reqs = await api("/signing/requests");
  stats();
  if (!changed("reqs", state.reqs)) return;
  table("requests", ["Document", "Signer", "Status", "Date", ""], state.reqs.map(r => el("tr", {},
    el("td", { class: "name" }, r.filename),
    el("td", {}, r.mode === "in_person" ? `In person${r.recipient_name ? ` · ${r.recipient_name}` : ""}` : r.recipient_email),
    el("td", {}, badge(r.status)), el("td", {}, fmtDate(r.signed_at || r.created_at)),
    el("td", { class: "actions" },
      r.status === "signed" && btn("Download signed", () => dl(r.signed_document_id, "signed.pdf")),
      r.status === "signed" && btn("Certificate", () => DM.download(`/signing/requests/${r.id}/certificate`, "certificate.pdf").catch(fail)),
      r.status === "pending" && r.mode === "email" && r.recipient_email && btn("Remind", async () => {
        try { await api(`/signing/requests/${r.id}/remind`, { method: "POST" }); toast(`Reminder queued for ${r.recipient_email}`, "ok"); } catch (e) { fail(e); }
      }),
      r.status === "pending" && btn("Cancel", async () => {
        if (!confirm("Cancel this signature request?")) return;
        try { await api(`/signing/requests/${r.id}/cancel`, { method: "POST" }); await loadRequests(); toast("Request cancelled"); } catch (e) { fail(e); }
      }, "danger"),
      btn("Audit trail", () => showAudit(r))))),
    ["No signature requests", "Open a document's More menu and choose Request signature."]);
}

const dlg = $("req-dialog");
let reqDoc = null;
function openRequest(d) {
  reqDoc = d; $("req-doc").textContent = d.filename;
  $("req-form").reset(); $("req-email-row").hidden = false; note($("req-msg"), "");
  $("req-submit").disabled = false; dlg.showModal();
}
$("req-mode").addEventListener("change", e => { $("req-email-row").hidden = e.target.value !== "email"; });
$("req-close").addEventListener("click", () => dlg.close());
$("req-form").addEventListener("submit", async e => {
  e.preventDefault();
  const mode = $("req-mode").value, m = $("req-msg");
  if (mode === "email" && !$("req-email").value) return note(m, "Enter the signer's email.", "error");
  $("req-submit").disabled = true;
  try {
    const r = await api("/signing/requests", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ document_id: reqDoc.id, mode, recipient_name: $("req-name").value, recipient_email: mode === "email" ? $("req-email").value : null }),
    });
    if (mode === "in_person") { location.href = r.signing_link; return; }
    $("req-form").replaceChildren(
      el("h2", {}, "Request created"),
      el("p", { class: `msg ${r.email_sent ? "ok" : ""}` }, r.email_sent
        ? `Invitation emailed to ${r.recipient_email}.`
        : "Email isn't configured (or failed). Share this link with the signer yourself. It is shown only once."),
      el("input", { type: "text", readonly: true, value: r.signing_link, onfocus: ev => ev.target.select() }),
      el("div", { class: "row", style: "margin-top:14px" }, el("button", { type: "button", onclick: () => dlg.close() }, "Done")));
    await loadRequests(); showTab("signatures");
  } catch (err) { note(m, err.message, "error"); $("req-submit").disabled = false; }
});

async function showAudit(r) {
  const body = $("audit-body"); body.replaceChildren(el("p", { class: "muted" }, "Loading…"));
  $("audit-dialog").showModal();
  try {
    const a = await api(`/signing/requests/${r.id}/events`);
    body.replaceChildren(
      el("p", { class: `msg ${a.chain_valid ? "ok" : "error"}` }, a.chain_valid ? "✓ Audit chain verified" : "Audit chain FAILED verification"),
      el("p", { class: "muted", style: "word-break:break-all" }, `Document SHA-256: ${a.document_hash}`, a.signed_file_hash ? el("br") : null, a.signed_file_hash ? `Signed SHA-256: ${a.signed_file_hash}` : null),
      el("div", { class: "table-wrap" }, el("table", {}, el("tbody", {}, a.events.map(ev => el("tr", {}, el("td", {}, fmtDate(ev.at)), el("td", {}, badge(ev.type)), el("td", { class: "muted", style: "word-break:break-all" }, ev.details)))))));
  } catch (e) { body.replaceChildren(el("p", { class: "msg error" }, e.message)); }
}
$("audit-close").addEventListener("click", () => $("audit-dialog").close());

// ---------- jobs ----------
let pollTimer;
async function loadJobs() {
  state.jobs = await api("/jobs");
  stats();
  if (changed("jobs", state.jobs)) renderJobs();
  clearTimeout(pollTimer);
  if (state.jobs.some(j => ["queued", "processing"].includes(j.status))) {
    pollTimer = setTimeout(() => Promise.all([loadJobs(), loadDocs()]).catch(() => {}), 3000);
  }
}
function renderJobs() {
  table("jobs", ["Document", "Service", "Status", "Result", ""], state.jobs.map(j => el("tr", {},
    el("td", { class: "name" }, j.filename), el("td", {}, SERVICE[j.service] || j.service), el("td", {}, badge(j.status)),
    el("td", { class: "muted" }, j.error || resultSummary(j)),
    el("td", { class: "actions" }, j.result_document_id && [
      btn("Download", () => dl(j.result_document_id, `${j.service}.pdf`)),
      j.service === "ocr" && btn("View text", () => showText({ id: j.result_document_id, filename: j.filename })),
    ]))),
    ["No jobs yet", "Run OCR or accessibility tagging from a document's More menu."]);
}
function resultSummary(j) {
  if (!j.report) return j.status === "completed" ? "" : "Waiting for worker…";
  if (j.service === "ocr") return `${j.report.lines} lines · ${Math.round(j.report.avg_confidence * 100)}% confidence`;
  return "Tagged · needs human review";
}

// ---------- text viewer ----------
let currentText = "";
async function showText(d) {
  $("text-title").textContent = d.filename; $("text-sub").textContent = "Loading…"; $("text-body").textContent = "";
  $("text-dialog").showModal();
  try {
    const t = await api(`/documents/${d.id}/text`);
    currentText = t.pages.map(p => (t.pages.length > 1 ? `--- Page ${p.page + 1} ---\n` : "") + p.text).join("\n\n");
    const empty = t.pages.every(p => !p.text);
    $("text-sub").textContent = t.source === "ocr" ? "Extracted by OCR" : empty ? "No text layer found. Run OCR on this document first." : "From the PDF's text layer";
    $("text-body").textContent = empty ? "" : currentText;
  } catch (e) { $("text-dialog").close(); fail(e); }
}
$("text-close").addEventListener("click", () => $("text-dialog").close());
$("text-copy").addEventListener("click", async () => {
  try { await navigator.clipboard.writeText(currentText); toast("Copied", "ok"); } catch { toast("Copy failed", "error"); }
});

// ---------- bulk sends ----------
async function loadBatches() {
  const list = await api("/bulk");
  if (!changed("batches", list)) return;
  table("batches", ["Name", "Progress", "Waiting", "Email problems", "Created", ""], list.map(b => el("tr", {},
    el("td", { class: "name", title: b.name }, b.name),
    el("td", {}, `${b.signed} / ${b.total} signed`), el("td", {}, String(b.pending)),
    el("td", {}, b.email_failed ? el("span", { class: "badge failed" }, String(b.email_failed)) : "-"),
    el("td", {}, fmtDate(b.created_at)),
    el("td", { class: "actions" }, el("a", { class: "btn sm", href: `/sends/${b.id}` }, "Open")))),
    ["No bulk sends yet", "Open a document's More menu and choose Send in bulk."]);
}

// ---------- email account banner ----------
async function loadMailBanner() {
  const m = await api("/email/settings"), b = $("mail-banner"), s = m.settings;
  if (m.source === "none") {
    b.className = "banner"; b.hidden = false;
    b.replaceChildren("Connect your email so signature requests are sent from your own address (Gmail, Outlook or any SMTP). ", el("a", { href: "/settings/email" }, "Set up email"));
  } else if (s && !s.verified_at) {
    b.className = "banner bad"; b.hidden = false;
    b.replaceChildren("Your email account needs attention: ", s.last_error || "not verified yet", ". ", el("a", { href: "/settings/email" }, "Fix it"));
  } else b.hidden = true;
}

// ---------- data sheets ----------
async function loadDatasets() {
  const list = await api("/datasets");
  if (!changed("datasets", list)) return;
  table("datasets", ["Name", "Columns", "Rows", "Added", ""], list.map(d => el("tr", {},
    el("td", { class: "name", title: d.name }, d.name),
    el("td", { class: "muted", title: d.columns.join(", ") }, d.columns.slice(0, 4).join(", ") + (d.columns.length > 4 ? ` +${d.columns.length - 4}` : "")),
    el("td", {}, String(d.row_count)), el("td", {}, fmtDate(d.created_at)),
    el("td", { class: "actions" }, btn("View", () => showDataset(d)),
      btn("Delete", async () => {
        if (!confirm(`Delete "${d.name}"? Templates using it will need another sheet.`)) return;
        try { await api(`/datasets/${d.id}`, { method: "DELETE" }); await loadDatasets(); toast("Data sheet deleted"); } catch (e) { fail(e); }
      }, "danger")))),
    ["No data sheets", "Upload an .xlsx or .csv file with your client data."]);
}
$("ds-upload").addEventListener("click", async () => {
  const f = $("ds-file").files[0], m = $("ds-msg");
  if (!f) return note(m, "Choose an .xlsx or .csv file first.", "error");
  const form = new FormData(); form.append("file", f); form.append("sheet", $("ds-sheet").value);
  $("ds-upload").disabled = true; note(m, "Uploading…");
  try {
    const d = await api("/datasets", { method: "POST", body: form });
    $("ds-file").value = ""; note(m, `Loaded ${d.row_count} rows and ${d.columns.length} columns.`, "ok"); await loadDatasets();
  } catch (e) { note(m, e.message, "error"); }
  $("ds-upload").disabled = false;
});
async function showDataset(d) {
  $("ds-title").textContent = d.name; $("ds-sub").textContent = `${d.row_count} rows · first 20 shown`;
  $("ds-body").replaceChildren(el("p", { class: "muted" }, "Loading…")); $("ds-dialog").showModal();
  try {
    const full = await api(`/datasets/${d.id}?limit=20`);
    $("ds-body").replaceChildren(el("table", {}, el("thead", {}, el("tr", {}, full.columns.map(c => el("th", {}, c)))),
      el("tbody", {}, full.rows.map(r => el("tr", {}, r.map(c => el("td", {}, c)))))));
  } catch (e) { $("ds-body").replaceChildren(el("p", { class: "msg error" }, e.message)); }
}
$("ds-close").addEventListener("click", () => $("ds-dialog").close());

// ---------- API keys ----------
async function loadKeys() {
  const keys = await api("/api-keys");
  table("keys", ["Name", "Key", "Last used", ""], keys.map(k => el("tr", {},
    el("td", {}, k.name), el("td", { class: "keyline" }, `${k.prefix}…`), el("td", {}, k.last_used_at ? fmtDate(k.last_used_at) : "never"),
    el("td", { class: "actions" }, btn("Revoke", async () => {
      if (!confirm(`Revoke "${k.name}"? Integrations using it will stop working.`)) return;
      try { await api(`/api-keys/${k.id}`, { method: "DELETE" }); await loadKeys(); toast("Key revoked"); } catch (e) { fail(e); }
    }, "danger")))),
    ["No API keys", "Create one to integrate with your systems."]);
}
$("key-create").addEventListener("click", async () => {
  const name = $("key-name").value.trim(), m = $("key-msg");
  if (!name) return note(m, "Give the key a name.", "error");
  try {
    const k = await api("/api-keys", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }) });
    $("key-name").value = "";
    m.className = "msg ok"; m.replaceChildren("Copy your key now. It won't be shown again:", el("span", { class: "secret keyline" }, k.key));
    await loadKeys();
  } catch (e) { note(m, e.message, "error"); }
});

// ---------- webhooks ----------
async function loadHooks() {
  const hooks = await api("/webhooks");
  table("hooks", ["URL", "Events", ""], hooks.map(h => el("tr", {},
    el("td", { style: "word-break:break-all" }, h.url), el("td", { class: "muted" }, h.events.join(", ")),
    el("td", { class: "actions" }, btn("Delete", async () => {
      if (!confirm("Delete this webhook?")) return;
      try { await api(`/webhooks/${h.id}`, { method: "DELETE" }); await loadHooks(); toast("Webhook deleted"); } catch (e) { fail(e); }
    }, "danger")))),
    ["No webhooks", "Add an endpoint to receive signing and job events."]);
}
$("hook-create").addEventListener("click", async () => {
  const url = $("hook-url").value.trim(), m = $("hook-msg");
  if (!url) return note(m, "Enter the endpoint URL.", "error");
  try {
    const h = await api("/webhooks", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ url }) });
    $("hook-url").value = "";
    m.className = "msg ok"; m.replaceChildren("Signing secret. Copy it now, it won't be shown again:", el("span", { class: "secret keyline" }, h.secret));
    await loadHooks();
  } catch (e) { note(m, e.message, "error"); }
});

Promise.all([loadDocs(), loadRequests(), loadJobs(), loadKeys(), loadHooks(), loadDatasets(), loadBatches(), loadMailBanner()]).catch(fail);
