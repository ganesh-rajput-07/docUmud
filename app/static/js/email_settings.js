const $ = id => document.getElementById(id);
const { api, toast, el } = DM;
let providers = [], current = null;

const note = (t, cls = "") => { $("msg").className = `msg ${cls}`; $("msg").textContent = t; };
const put = body => ({ method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

function showProvider(id) {
  const p = providers.find(x => x.id === id);
  $("provider-help").hidden = !p;
  if (!p) return;
  $("provider-help").textContent = p.help;
  if (p.id !== "custom") {
    $("host").value = p.host; $("port").value = p.port; $("security").value = p.security; $("daily_limit").value = p.daily_limit;
  }
}
$("provider").addEventListener("change", e => showProvider(e.target.value));
$("security").addEventListener("change", e => { if (["587", "465", "25"].includes($("port").value)) $("port").value = e.target.value === "ssl" ? 465 : 587; });
$("pw-toggle").addEventListener("click", () => {
  const show = $("password").type === "password";
  $("password").type = show ? "text" : "password"; $("pw-toggle").textContent = show ? "Hide" : "Show";
});

function render(view) {
  current = view; const s = view.settings;
  const badge = $("status-badge");
  if (s) {
    badge.className = `badge ${s.verified_at ? "signed" : "failed"}`; badge.textContent = s.verified_at ? "Connected ✓" : "Needs attention";
    $("host").value = s.host; $("port").value = s.port; $("security").value = s.security; $("username").value = s.username;
    $("from_name").value = s.from_name; $("from_email").value = s.from_email === s.username ? "" : s.from_email; $("daily_limit").value = s.daily_limit;
    $("password").placeholder = s.has_password ? "•••••••• (saved. Leave blank to keep)" : "";
    $("provider").value = providers.find(p => p.host === s.host)?.id || "custom"; showProvider($("provider").value);
    $("remove").hidden = false;
    if (s.last_error) note(s.last_error, "error");
  } else {
    badge.className = "badge neutral"; badge.textContent = view.source === "platform" ? "Using platform default" : "Not connected";
    $("provider").value = "gmail"; showProvider("gmail"); $("remove").hidden = true;
  }
}

$("smtp-form").addEventListener("submit", async e => {
  e.preventDefault();
  const body = { host: $("host").value.trim(), port: +$("port").value, security: $("security").value, username: $("username").value.trim(),
    from_name: $("from_name").value.trim(), from_email: $("from_email").value.trim(), daily_limit: +$("daily_limit").value || 0 };
  if ($("password").value) body.password = $("password").value;
  $("save").disabled = true; note("Saving and checking the login…");
  try {
    const view = await api("/email/settings", put(body));
    $("password").value = ""; render(view);
    if (view.settings.verified_at) { note("Connected. Your email account works.", "ok"); toast("Email account connected", "ok"); }
  } catch (err) { note(err.message, "error"); }
  $("save").disabled = false;
});

$("test").addEventListener("click", async () => {
  $("test").disabled = true; note("Sending a test email…");
  try {
    const r = await api("/email/test", { method: "POST" });
    note(`Test email sent to ${r.sent_to}. Check your inbox.`, "ok");
    render(await api("/email/settings"));
  } catch (err) { note(err.message, "error"); }
  $("test").disabled = false;
});

$("remove").addEventListener("click", async () => {
  if (!confirm("Remove your email account? Invitations will no longer be sent from your address.")) return;
  try { await api("/email/settings", { method: "DELETE" }); $("smtp-form").reset(); note(""); render(await api("/email/settings")); toast("Email account removed"); }
  catch (err) { note(err.message, "error"); }
});

(async () => {
  try {
    providers = await api("/email/providers");
    $("provider").replaceChildren(...providers.map(p => el("option", { value: p.id }, p.label)));
    render(await api("/email/settings"));
  } catch (e) { note(e.message, "error"); }
})();
