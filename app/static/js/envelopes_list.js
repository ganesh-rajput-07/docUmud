const $ = id => document.getElementById(id);
const { api, el, badge, fmtDate, toast } = DM;
const STATUSES = [["", "All"], ["in_progress", "In progress"], ["completed", "Completed"], ["draft", "Drafts"], ["declined", "Declined"], ["cancelled", "Cancelled"], ["expired", "Expired"]];
const state = { status: "", q: "", template: "", after: "", before: "", cursor: null, rows: [], selected: new Set(), templates: {} };
let timer;

function params(extra = {}) {
  const p = new URLSearchParams({ limit: 50, ...extra });
  if (state.status) p.set("status", state.status); if (state.q) p.set("q", state.q); if (state.template) p.set("template_id", state.template);
  if (state.after) p.set("created_after", new Date(state.after + "T00:00:00").toISOString());
  if (state.before) p.set("created_before", new Date(state.before + "T23:59:59").toISOString());
  return p;
}
async function loadStats() {
  const s = await api("/v1/envelopes/stats");
  $("stats").replaceChildren(...STATUSES.map(([k, label]) => el("div", { class: "stat", role: "button", tabindex: 0, "aria-pressed": String(state.status === k), onclick: () => { state.status = state.status === k ? "" : k; refresh(); } },
    el("b", {}, String(k ? s[k] : s.total)), el("span", {}, label))));
}
async function load(reset) {
  if (reset) { state.cursor = null; state.rows = []; }
  const r = await api(`/v1/envelopes?${params(state.cursor ? { cursor: state.cursor } : {})}`);
  state.rows.push(...r.data); state.cursor = r.next_cursor; $("more").hidden = !r.has_more; render();
}
function render() {
  const box = $("rows"); box.replaceChildren();
  if (!state.rows.length) return box.append(el("div", { class: "empty" }, el("b", {}, "No envelopes found"), state.q || state.status || state.template ? "Try different filters." : "Create one from a template."));
  const all = el("input", { type: "checkbox", "aria-label": "Select all", checked: state.rows.every(r => state.selected.has(r.id)), onchange: e => { state.rows.forEach(r => e.target.checked ? state.selected.add(r.id) : state.selected.delete(r.id)); render(); bulk(); } });
  box.append(el("div", { class: "table-wrap" }, el("table", {},
    el("thead", {}, el("tr", {}, el("th", { style: "width:28px" }, all), ...["Name", "Template", "Signers", "Status", "Created", ""].map(h => el("th", { class: h === "" ? "end" : "" }, h)))),
    el("tbody", {}, state.rows.map(r => el("tr", {},
      el("td", {}, el("input", { type: "checkbox", "aria-label": `Select ${r.name}`, checked: state.selected.has(r.id), onchange: e => { e.target.checked ? state.selected.add(r.id) : state.selected.delete(r.id); bulk(); } })),
      el("td", { class: "name", title: r.name }, el("a", { href: `/envelopes/${r.id}` }, r.name), r.external_id ? el("div", { class: "muted" }, r.external_id) : null),
      el("td", { class: "muted" }, state.templates[r.template_id] || "-"),
      el("td", {}, el("span", { class: "mini" }, el("i", { style: `width:${r.total ? 100 * r.signed / r.total : 0}%` })), `${r.signed}/${r.total}`,
        el("div", { class: "muted", title: r.recipients.map(x => `${x.role}: ${x.email || x.name} (${x.status})`).join("\n") }, r.recipients.filter(x => x.email).slice(0, 2).map(x => x.email).join(", "))),
      el("td", {}, badge(r.status)), el("td", {}, fmtDate(r.created_at)),
      el("td", { class: "actions" }, el("a", { class: "btn sm", href: `/envelopes/${r.id}` }, "Open"))))))));
}
function bulk() { const n = state.selected.size; $("bulkbar").hidden = !n; $("sel-count").textContent = `${n} selected`; }
async function each(path, verb) {
  const ids = [...state.selected]; let ok = 0, failed = 0;
  for (const id of ids) { try { await api(`/v1/envelopes/${id}/${path}`, { method: "POST" }); ok++; } catch { failed++; } }
  toast(`${verb}: ${ok} done${failed ? `, ${failed} skipped` : ""}`, failed ? "error" : "ok"); state.selected.clear(); bulk(); refresh();
}
$("b-remind").addEventListener("click", () => each("remind", "Reminders queued"));
$("b-cancel").addEventListener("click", () => { if (confirm(`Cancel ${state.selected.size} envelope(s)? Their links stop working.`)) each("cancel", "Cancelled"); });
$("b-none").addEventListener("click", () => { state.selected.clear(); render(); bulk(); });
async function refresh() { try { await Promise.all([loadStats(), load(true)]); } catch (e) { toast(e.message, "error"); } }
$("q").addEventListener("input", e => { clearTimeout(timer); timer = setTimeout(() => { state.q = e.target.value.trim(); load(true).catch(x => toast(x.message, "error")); }, 300); });
$("f-template").addEventListener("change", e => { state.template = e.target.value; load(true); });
$("f-after").addEventListener("change", e => { state.after = e.target.value; load(true); });
$("f-before").addEventListener("change", e => { state.before = e.target.value; load(true); });
$("clear").addEventListener("click", () => { Object.assign(state, { status: "", q: "", template: "", after: "", before: "" }); $("q").value = ""; $("f-template").value = ""; $("f-after").value = $("f-before").value = ""; refresh(); });
$("more").addEventListener("click", () => load(false).catch(e => toast(e.message, "error")));
(async () => {
  const { data } = await api("/v1/templates?status=all"); data.forEach(t => { state.templates[t.id] = t.name; });
  $("f-template").replaceChildren(el("option", { value: "" }, "All templates"), ...data.map(t => el("option", { value: t.id }, t.name)));
  await refresh(); setInterval(() => { if (state.rows.some(r => r.status === "in_progress") && !document.hidden) loadStats().catch(() => {}); }, 20000);
})();
