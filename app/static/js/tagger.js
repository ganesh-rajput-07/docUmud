const $ = id => document.getElementById(id);
const { el, api } = DM;
const base = `/documents/${window.TAGGER.documentId}`;
let tags = [], page = 0, pages = 1, filename = "document", selected = null;
let start = null, lastUrl = null;

const setStatus = (t, cls = "") => { $("status").className = `msg ${cls}`; $("status").textContent = t; };
const guard = fn => async (...a) => { try { await fn(...a); setStatus(""); } catch (e) { setStatus(e.message, "error"); } };
const byId = id => tags.find(t => t.id === id);
const clamp = (v, lo, hi) => Math.min(Math.max(v, lo), hi);

function render() {
  const overlay = $("overlay"); overlay.replaceChildren();
  tags.filter(t => t.page === page).forEach(t => {
    const box = el("div", { class: `tagbox${t.id === selected ? " sel" : ""}`, title: t.label,
      style: `left:${t.x * 100}%;top:${t.y * 100}%;width:${t.w * 100}%;height:${t.h * 100}%` }, el("span", {}, t.label));
    box.addEventListener("pointerdown", e => { e.stopPropagation(); select(t.id); });
    overlay.append(box);
  });
  $("count").textContent = `(${tags.length})`;
  const list = $("tag-list"); list.replaceChildren();
  if (!tags.length) list.append(el("p", { class: "empty" }, "No tags yet."));
  tags.forEach(t => list.append(el("button", { class: `sm${t.id === selected ? " sel" : ""}`, onclick: () => { page = t.page; loadPage(); select(t.id); } },
    `p${t.page + 1} · ${t.label}${t.value ? ` = ${t.value}` : ""}`)));
  const t = byId(selected);
  $("editor").hidden = !t;
  if (t) { $("t-label").value = t.label; $("t-value").value = t.value; }
}
function select(id) { selected = id; render(); }

async function loadPage() {
  $("page-label").textContent = `Page ${page + 1} of ${pages}`;
  $("prev").disabled = page === 0; $("next").disabled = page >= pages - 1;
  try {
    const url = await DM.blobUrl(`${base}/pages/${page}.png`);
    if (lastUrl) URL.revokeObjectURL(lastUrl);
    lastUrl = url; $("page-img").src = url;
  } catch (e) { setStatus(e.message, "error"); }
  render();
}
$("prev").addEventListener("click", () => { page--; loadPage(); });
$("next").addEventListener("click", () => { page++; loadPage(); });

// ---- draw a new tag ----
const wrap = $("pagewrap"), draft = $("draft");
const point = e => { const r = $("page-img").getBoundingClientRect(); return { x: clamp((e.clientX - r.left) / r.width, 0, 1), y: clamp((e.clientY - r.top) / r.height, 0, 1) }; };
const rectOf = (a, b) => ({ x: Math.min(a.x, b.x), y: Math.min(a.y, b.y), w: Math.abs(a.x - b.x), h: Math.abs(a.y - b.y) });
wrap.addEventListener("pointerdown", e => { start = point(e); wrap.setPointerCapture(e.pointerId); });
wrap.addEventListener("pointermove", e => {
  if (!start) return;
  const r = rectOf(start, point(e));
  Object.assign(draft.style, { left: `${r.x * 100}%`, top: `${r.y * 100}%`, width: `${r.w * 100}%`, height: `${r.h * 100}%` });
  draft.hidden = false;
});
wrap.addEventListener("pointerup", guard(async e => {
  if (!start) return;
  const r = rectOf(start, point(e)); start = null; draft.hidden = true;
  if (r.w < 0.01 || r.h < 0.005) { select(null); return; }  // a click, not a drag
  const label = $("default-label").value.trim() || "Tag";
  const tag = await api(`${base}/tags`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ page, ...r, label }) });
  tags.push(tag); select(tag.id);
}));
wrap.addEventListener("pointercancel", () => { start = null; draft.hidden = true; });

// ---- edit / delete ----
const patch = guard(async body => {
  const t = byId(selected); if (!t) return;
  Object.assign(t, await api(`${base}/tags/${t.id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }));
  render();
});
$("t-label").addEventListener("change", () => { const v = $("t-label").value.trim(); if (v) patch({ label: v }); else render(); });
$("t-value").addEventListener("change", () => patch({ value: $("t-value").value }));
$("t-delete").addEventListener("click", guard(async () => {
  const t = byId(selected); if (!t) return;
  await api(`${base}/tags/${t.id}`, { method: "DELETE" });
  tags = tags.filter(x => x.id !== t.id); select(null);
}));

$("export").addEventListener("click", () => {
  const blob = new Blob([JSON.stringify({ document: filename, tags }, null, 2)], { type: "application/json" });
  const a = Object.assign(document.createElement("a"), { href: URL.createObjectURL(blob), download: `${filename.replace(/\.pdf$/i, "")}-tags.json` });
  a.click(); URL.revokeObjectURL(a.href);
});

$("export-pdf").addEventListener("click", guard(async () => {
  $("export-pdf").disabled = true;
  try {
    const doc = await api(`${base}/tags/export`, { method: "POST" });
    setStatus(`Saved as “${doc.filename}” in your documents.`, "ok");
  } finally { $("export-pdf").disabled = false; }
}));

$("fill-link").href = `${base}/fill`;

async function loadColumns() {
  const sheets = await api("/datasets");
  const cols = [...new Set(sheets.flatMap(d => d.columns))];
  if (!cols.length) return;
  $("cols-card").hidden = false;
  const input = $("default-label");
  const chips = cols.map(c => el("button", { type: "button", class: "chip sm", "aria-pressed": String(input.value === c), onclick: () => {
    input.value = c; chips.forEach(x => x.setAttribute("aria-pressed", String(x === chips.find(y => y.textContent === c))));
  } }, c));
  $("chips").replaceChildren(...chips);
  $("label-presets").append(...cols.map(c => el("option", { value: c })));
}

guard(async () => {
  loadColumns().catch(() => {});
  const data = await api(`${base}/tags`);
  tags = data.tags; pages = data.pages; filename = data.filename;
  $("doc-title").textContent = `Tag data · ${filename}`;
  const docs = await api("/documents");
  $("doc-switch").replaceChildren(...docs.map(d => el("option", { value: d.id, selected: d.id === window.TAGGER.documentId }, d.filename)));
  $("doc-switch").addEventListener("change", e => { location.href = `/documents/${e.target.value}/tagger`; });
  await loadPage();
})();
