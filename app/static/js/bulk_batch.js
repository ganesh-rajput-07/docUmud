const $ = id => document.getElementById(id);
const { api, el, badge, fmtDate, toast } = DM;
const id = window.BATCH.id;
let data = null, last = "", timer;

const SIGN = { signed: "signed", pending: "pending", cancelled: "cancelled", expired: "expired", signing: "pending" };
const MAIL = { sent: ["Delivered", "signed"], pending: ["Queued", "pending"], failed: ["Failed", "failed"], not_configured: ["Not sent", "neutral"], cancelled: ["Cancelled", "cancelled"], none: ["-", "neutral"] };
const post = path => ({ method: "POST" });
const act = async (path, ok) => { try { const r = await api(`/bulk/${id}/${path}`, { method: "POST" }); toast(ok(r), "ok"); await load(true); } catch (e) { toast(e.message, "error"); } };
const dl = (path, name) => DM.download(`/bulk/${id}/${path}`, name).catch(e => toast(e.message, "error"));

function render() {
  const c = data.counts;
  $("title").textContent = data.name;
  $("sub").textContent = `Created ${fmtDate(data.created_at)} · links valid ${data.expires_days} days${data.personalize ? " · personalised copies" : ""}`;
  $("t-total").textContent = c.total; $("t-signed").textContent = c.signed; $("t-pending").textContent = c.pending;
  $("t-problems").textContent = c.email_failed;
  $("bar").style.width = `${c.total ? Math.round(100 * c.signed / c.total) : 0}%`;
  $("retry").hidden = !c.email_failed;
  $("remind").disabled = !c.pending; $("cancel").disabled = !c.pending; $("dl-signed").disabled = !c.signed;

  const banner = $("banner");
  if (!data.email_configured || c.email_not_configured) {
    banner.className = "banner"; banner.hidden = false;
    banner.replaceChildren("Emails were not sent because no email account is connected. ", el("a", { href: "/settings/email" }, "Connect one"),
      " or use “Signing links (CSV)” to share the links another way.");
  } else if (c.email_failed) {
    banner.className = "banner bad"; banner.hidden = false;
    banner.replaceChildren(`${c.email_failed} email(s) couldn't be delivered. `, el("a", { href: "/settings/email" }, "Check your email settings"), " then use “Retry failed emails”.");
  } else banner.hidden = true;

  const rows = data.items.map(i => {
    const [mlabel, mcls] = MAIL[i.email_status] || MAIL.none;
    return el("tr", {},
      el("td", {}, String(i.row)), el("td", { class: "name", title: i.name }, i.name || "-"), el("td", {}, i.email),
      el("td", {}, badge(SIGN[i.status] || i.status)),
      el("td", { title: i.email_error || "" }, el("span", { class: `badge ${mcls}` }, mlabel)),
      el("td", {}, i.reminders ? String(i.reminders) : "-"),
      el("td", {}, i.signed_at ? fmtDate(i.signed_at) : "-"),
      el("td", { class: "actions" }, i.status === "pending" && el("button", { class: "sm", onclick: async () => {
        try { await api(`/bulk/${id}/items/${i.item_id}/resend`, { method: "POST" }); toast(`Reminder queued for ${i.email}`, "ok"); await load(true); } catch (e) { toast(e.message, "error"); }
      } }, "Resend")));
  });
  $("items").replaceChildren(el("div", { class: "table-wrap" }, el("table", {},
    el("thead", {}, el("tr", {}, ["Row", "Name", "Email", "Signing", "Email", "Reminders", "Signed", ""].map(h => el("th", { class: h === "" ? "end" : "" }, h)))),
    el("tbody", {}, rows))));
}

async function load(force) {
  data = await api(`/bulk/${id}`);
  const sig = JSON.stringify(data);
  if (force || sig !== last) { last = sig; render(); }
  clearTimeout(timer);
  if (data.counts.pending || data.counts.email_pending) timer = setTimeout(() => load().catch(() => {}), 5000);
}

$("remind").addEventListener("click", () => act("remind", r => `${r.queued} reminder(s) queued${r.skipped_recently_emailed ? `, ${r.skipped_recently_emailed} skipped (emailed within the last hour)` : ""}`));
$("retry").addEventListener("click", () => act("retry-failed", r => `${r.queued} email(s) re-queued`));
$("cancel").addEventListener("click", () => { if (confirm("Cancel every request that hasn't been signed yet? Their links will stop working.")) act("cancel", r => `${r.cancelled} request(s) cancelled`); });
$("dl-signed").addEventListener("click", () => dl("signed.zip", "signed.zip"));
$("dl-csv").addEventListener("click", () => dl("export.csv", "status.csv"));
$("dl-links").addEventListener("click", () => dl("links.csv", "links.csv"));

load(true).catch(e => toast(e.message, "error"));
