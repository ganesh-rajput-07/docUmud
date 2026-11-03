const $ = id => document.getElementById(id);
const { api, el, badge, fmtDate, toast } = DM;
const id = window.ENV.id, base = `/v1/envelopes/${id}`;
let env = null, timer, last = "";
const post = (path, ok) => async () => { try { const r = await api(`${base}/${path}`, { method: "POST" }); toast(ok(r), "ok"); await load(true); } catch (e) { toast(e.message, "error"); } };
const dl = (path, name) => DM.download(path, name).catch(e => toast(e.message, "error"));
const STATE = { signed: "signed", pending: "pending", viewed: "pending", waiting: "queued", declined: "failed" };

async function showUrl(r) {
  try {
    const { url } = await api(`${base}/recipients/${r.id}/signing-url`, { method: "POST" });
    $("url-who").textContent = `${r.role}${r.name ? ` · ${r.name}` : ""}. Anyone with this link can sign for them. Share it only with them.`; $("url-box").value = url; $("url-open").href = url; $("url-dialog").showModal(); $("url-box").select();
  } catch (e) { toast(e.message, "error"); }
}
$("url-copy").addEventListener("click", async () => { try { await navigator.clipboard.writeText($("url-box").value); toast("Link copied", "ok"); } catch { toast("Copy failed", "error"); } });
$("url-close").addEventListener("click", () => $("url-dialog").close());

function render(ev) {
  $("title").textContent = env.name; $("status").replaceChildren(badge(env.status));
  $("sub").replaceChildren(`Created ${fmtDate(env.created_at)} · expires ${fmtDate(env.expires_at)}${env.external_id ? ` · ref ${env.external_id}` : ""}`,
    ...(env.template_id ? [" · ", el("a", { href: `/templates/${env.template_id}/design` }, "template")] : []));
  const office = env.recipients.find(r => r.kind === "office" && r.signing_url);
  const a = [];
  if (office && env.status === "in_progress") a.push(el("a", { class: "btn primary", href: office.signing_url }, "Sign as office"));
  if (env.status === "draft") a.push(el("button", { class: "primary", onclick: post("send", () => "Envelope sent") }, "Send"));
  if (env.status === "in_progress") a.push(el("button", { onclick: post("remind", r => r.queued ? `${r.queued} reminder(s) queued` : "Nobody to remind (or emailed within the last hour)") }, "Remind"));
  a.push(el("button", { onclick: () => dl(`${base}/document`, `${env.name}.pdf`) }, env.status === "completed" ? "Download signed PDF" : "Preview PDF"));
  if (env.status === "completed") a.push(el("button", { onclick: () => dl(`${base}/certificate`, "certificate.pdf") }, "Certificate"));
  if (!["completed", "declined", "cancelled", "expired"].includes(env.status)) a.push(el("button", { class: "danger", onclick: () => confirm("Void this envelope? Every link stops working.") && post("cancel", () => "Envelope cancelled")() }, "Void"));
  $("actions").replaceChildren(...a);
  const failed = (env.emails || []).filter(m => m.status === "failed"), none = (env.emails || []).filter(m => m.status === "not_configured");
  const b = $("banner");
  if (failed.length) { b.className = "banner bad"; b.hidden = false; b.replaceChildren(`${failed.length} email(s) failed: ${failed[0].error}. `, el("a", { href: "/settings/email" }, "Check email settings")); }
  else if (none.length && env.status === "in_progress") { b.className = "banner"; b.hidden = false; b.replaceChildren("Emails weren't sent because no email account is connected. Use “Signing link” below to share each link yourself, or ", el("a", { href: "/settings/email" }, "connect your mailbox"), "."); }
  else if (env.status === "declined") { const d = env.recipients.find(r => r.status === "declined"); b.className = "banner bad"; b.hidden = false; b.textContent = `${d?.role || "A recipient"} declined to sign.`; }
  else b.hidden = true;

  $("recipients").replaceChildren(el("div", { class: "table-wrap" }, el("table", {}, el("thead", {}, el("tr", {}, ["Role", "Name", "Email", "Status", "Signed", ""].map(h => el("th", { class: h === "" ? "end" : "" }, h)))),
    el("tbody", {}, env.recipients.map(r => el("tr", {}, el("td", {}, DM.humanize(r.role) + (r.kind === "cc" && r.role !== "cc" ? " (cc)" : "")), el("td", {}, r.name || "-"), el("td", {}, r.email || (r.deliver === "none" ? "embedded" : "-")),
      el("td", {}, r.kind === "cc" ? el("span", { class: "badge neutral" }, "copy") : badge(STATE[r.status] || r.status)), el("td", {}, r.signed_at ? fmtDate(r.signed_at) : "-"),
      el("td", { class: "actions" }, r.signing_url && el("button", { class: "sm", onclick: () => showUrl(r) }, "Signing link"))))))));
  const fields = env.fields.filter((f, i, all) => f.type !== "radio" || all.findIndex(x => x.name === f.name) === i);
  $("fields").replaceChildren(el("div", { class: "table-wrap" }, el("table", {}, el("thead", {}, el("tr", {}, ["Field", "Type", "Role", "Value"].map(h => el("th", {}, h)))),
    el("tbody", {}, fields.map(f => el("tr", {}, el("td", { class: "name", title: f.name }, f.label || DM.humanize(f.name)), el("td", { class: "muted" }, f.type.replace("_", " ")), el("td", {}, f.role),
      el("td", { class: "name" }, f.signed ? "✓ signed" : f.value || el("span", { class: "muted" }, f.locked ? "-" : "waiting"))))))));
  if (ev) $("events").replaceChildren(el("p", { class: `msg ${ev.chain_valid ? "ok" : "error"}` }, ev.chain_valid ? "✓ Audit trail verified" : "Audit trail FAILED verification"),
    ...ev.events.map(e => el("div", { style: "padding:6px 0;border-bottom:1px solid var(--line)" }, el("b", {}, e.type.replace(/_/g, " ")), el("span", { class: "muted" }, ` · ${fmtDate(e.at)}`), e.details ? el("div", { class: "muted", style: "word-break:break-all;font-size:12.5px" }, e.details) : null)));
  $("mail-card").hidden = !(env.emails || []).length;
  const mb = s => el("span", { class: `badge ${{ sent: "signed", failed: "failed", pending: "pending" }[s] || "neutral"}` }, s.replace("_", " "));
  $("mail").replaceChildren(el("div", { class: "table-wrap" }, el("table", {}, el("tbody", {}, (env.emails || []).map(m => el("tr", {}, el("td", {}, m.kind), el("td", {}, m.to), el("td", {}, mb(m.status)), el("td", { class: "muted" }, m.error || "")))))));
}
async function load(force) {
  env = await api(base); const ev = await api(`${base}/events`);
  const sig = JSON.stringify([env, ev]); if (force || sig !== last) { last = sig; render(ev); }
  clearTimeout(timer); if (env.status === "in_progress") timer = setTimeout(() => load().catch(() => {}), 8000);
}
load(true).catch(e => toast(e.message, "error"));
