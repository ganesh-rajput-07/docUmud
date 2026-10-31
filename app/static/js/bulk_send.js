const $ = id => document.getElementById(id);
const { api, el, toast } = DM;
const documentId = window.BULK.documentId;
let doc, tpl, datasets = [], mail, timer, plan = null;

const note = (t, cls = "") => { $("send-msg").className = `msg ${cls}`; $("send-msg").textContent = t; };
const post = body => ({ method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
const dataset = () => datasets.find(d => d.id === $("ds").value);
const guess = (cols, re) => cols.find(c => re.test(c)) || "";

function renderBanner() {
  const b = $("mail-banner"), s = mail.settings;
  if (mail.source === "none") {
    b.className = "banner"; b.hidden = false;
    b.replaceChildren("No email account is connected, so signing links will be created but not emailed. ", el("a", { href: "/settings/email" }, "Connect Gmail or any SMTP"), " or share the links yourself afterwards.");
  } else if (s && !s.verified_at) {
    b.className = "banner bad"; b.hidden = false;
    b.replaceChildren("Your email account needs attention: ", s.last_error || "not verified yet", " ", el("a", { href: "/settings/email" }, "Fix it"));
  } else b.hidden = true;
  $("send-from").textContent = mail.source === "user" ? `Emails will be sent from ${mail.settings.from_email} (your account).`
    : mail.source === "platform" ? "Emails will be sent from the platform's default address." : "Nothing will be emailed until you connect an account.";
}

function renderColumns() {
  const ds = dataset(), cols = ds?.columns || [];
  $("email-col").replaceChildren(...cols.map(c => el("option", { value: c }, c)));
  $("name-col").replaceChildren(el("option", { value: "" }, "(none)"), ...cols.map(c => el("option", { value: c }, c)));
  $("email-col").value = guess(cols, /e-?mail|mail/i) || cols[0] || "";
  $("name-col").value = guess(cols, /name/i);
  $("from-row").value = 1; $("to-row").value = ds?.row_count || "";
  schedule();
}

function body() {
  return { document_id: documentId, dataset_id: $("ds").value, email_column: $("email-col").value, name_column: $("name-col").value || null,
    personalize: $("personalize").checked, name: $("batch-name").value.trim(), subject: $("subject").value, message: $("message").value,
    expires_days: +$("expires").value, start: +$("from-row").value || 1, end: +$("to-row").value || null };
}

function schedule() { clearTimeout(timer); timer = setTimeout(check, 350); }
async function check() {
  const box = $("check"), send = $("send");
  plan = null; send.disabled = true;
  if (!$("ds").value || !$("email-col").value) { box.replaceChildren(el("span", { class: "muted" }, "Choose a data sheet and the column that holds email addresses.")); send.textContent = "Send"; return; }
  try {
    plan = await api("/bulk/preview", post(body()));
  } catch (e) { box.replaceChildren(el("span", { class: "msg error" }, e.message)); return; }
  const parts = [el("div", { class: "ok" }, `✓ ${plan.valid} people will receive this`)];
  if (plan.over_limit) parts.push(el("div", { class: "warn" }, "That's more than one send allows. Narrow the row range."));
  if (plan.invalid_count) parts.push(el("div", { class: "warn" }, `${plan.invalid_count} row(s) skipped: not a valid email`),
    el("ul", {}, plan.invalid.map(i => el("li", {}, `Row ${i.row}: ${i.reason}${i.value ? ` (“${i.value}”)` : ""}`))));
  if (plan.duplicate_count) parts.push(el("div", { class: "warn" }, `${plan.duplicate_count} duplicate address(es) skipped`),
    el("ul", {}, plan.duplicates.map(i => el("li", {}, `Row ${i.row}: ${i.value}`))));
  if (plan.sample.length) parts.push(el("div", { class: "muted", style: "margin-top:8px" }, "First recipients: " + plan.sample.map(s => s.name ? `${s.name} <${s.email}>` : s.email).join(", ")));
  if (plan.daily_limit && plan.valid > plan.daily_limit) parts.push(el("div", { class: "warn" }, `Your account sends up to ${plan.daily_limit} emails a day, so the rest will go out over the next days automatically.`));
  box.replaceChildren(...parts);
  send.disabled = plan.valid === 0 || plan.over_limit;
  send.textContent = plan.valid ? `Send to ${plan.valid} ${plan.valid === 1 ? "person" : "people"}` : "No valid recipients";
}

["ds"].forEach(id => $(id).addEventListener("change", renderColumns));
["email-col", "name-col", "from-row", "to-row", "personalize"].forEach(id => $(id).addEventListener("input", schedule));

$("ds-upload").addEventListener("click", () => $("ds-file").click());
$("ds-file").addEventListener("change", async e => {
  const file = e.target.files[0]; e.target.value = "";
  if (!file) return;
  const form = new FormData(); form.append("file", file);
  try {
    const ds = await api("/datasets", { method: "POST", body: form });
    datasets = await api("/datasets"); renderDatasets(ds.id); renderColumns(); toast(`Loaded ${ds.row_count} rows`, "ok");
  } catch (err) { toast(err.message, "error"); }
});

function renderDatasets(selected) {
  $("ds").replaceChildren(el("option", { value: "" }, "Choose a sheet…"),
    ...datasets.map(d => el("option", { value: d.id }, `${d.name} · ${d.row_count} rows`)));
  $("ds").value = selected || "";
}

$("send").addEventListener("click", async () => {
  if (!plan?.valid) return;
  const when = mail.source === "none" ? " No emails will be sent (no email account connected); you can export the links afterwards." : "";
  if (!confirm(`Send “${doc.filename}” to ${plan.valid} ${plan.valid === 1 ? "person" : "people"}?${when}`)) return;
  $("send").disabled = true; note("Creating requests… this can take a moment for large sends.");
  try {
    const r = await api("/bulk", post(body()));
    location.href = `/sends/${r.id}`;
  } catch (e) { note(e.message, "error"); $("send").disabled = false; }
});

(async () => {
  try {
    const docs = await api("/documents");
    [tpl, datasets, mail] = await Promise.all([api(`/documents/${documentId}/template`), api("/datasets"), api("/email/settings")]);
    doc = docs.find(d => d.id === documentId);
    if (!doc) throw new Error("Document not found.");
    $("title").textContent = `Send in bulk · ${doc.filename}`;
    $("batch-name").value = `${doc.filename.replace(/\.pdf$/i, "")} signatures`;
    const hasTags = tpl.fields.length > 0;
    $("personalize").disabled = !hasTags;
    $("pers-note").replaceChildren(hasTags
      ? el("span", {}, "Each person's copy is filled using the font, style and field matching saved in ", el("a", { href: `/documents/${documentId}/fill` }, "Fill from data"), ".")
      : el("span", {}, "To fill each copy from the sheet, ", el("a", { href: `/documents/${documentId}/tagger` }, "tag the fields"), " first. Otherwise everyone signs the same document."));
    renderBanner(); renderDatasets(tpl.dataset?.id); renderColumns();
  } catch (e) { $("check").textContent = e.message; $("check").className = "msg error"; }
})();
