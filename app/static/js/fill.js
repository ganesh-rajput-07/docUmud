// Fill-from-data screen. Everything the user picks is saved to the document's template as they go,
// so the next run starts exactly where this one left off.
const $ = id => document.getElementById(id);
const { el, api, toast } = DM;
const base = `/documents/${window.FILL.documentId}`;
const json = body => ({ method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

let tpl = null, datasets = [], catalog = null;
let row = 1, page = 0, previewUrl = null, previewSeq = 0, previewTimer;

const rowCount = () => tpl.dataset?.row_count || 0;
const columns = () => tpl.dataset?.columns || [];
const fail = e => toast(e.message, "error");
const note = (id, text, cls = "") => { const n = $(id); n.className = `msg ${cls}`; n.textContent = text; };

// ---------- save ----------
async function save(patch) {
  try { tpl = await api(`${base}/template`, json(patch)); }
  catch (e) { fail(e); return false; }
  renderAll(); schedulePreview();
  return true;
}

// ---------- preview ----------
function schedulePreview() { clearTimeout(previewTimer); previewTimer = setTimeout(refreshPreview, 200); }
async function refreshPreview() {
  const seq = ++previewSeq;
  const n = rowCount();
  row = Math.min(Math.max(row, 1), Math.max(n, 1));
  $("row-label").textContent = n ? `Row ${row} of ${n}` : "No sheet chosen";
  $("page-label").textContent = `Page ${page + 1} of ${tpl.pages}`;
  $("row-prev").disabled = row <= 1; $("row-next").disabled = row >= n;
  $("page-prev").disabled = page === 0; $("page-next").disabled = page >= tpl.pages - 1;
  $("preview-note").textContent = n ? "" : "Showing field names. Choose a data sheet to preview real values.";
  try {
    const url = await DM.blobUrl(`${base}/fill/preview.png?row=${row}&page=${page}`);
    if (seq !== previewSeq) return URL.revokeObjectURL(url);
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    previewUrl = url; $("preview").src = url;
  } catch (e) { if (seq === previewSeq) $("preview-note").textContent = e.message; }
}
$("row-prev").addEventListener("click", () => { row--; refreshPreview(); });
$("row-next").addEventListener("click", () => { row++; refreshPreview(); });
$("page-prev").addEventListener("click", () => { page--; refreshPreview(); });
$("page-next").addEventListener("click", () => { page++; refreshPreview(); });

// ---------- 1 · data sheet ----------
function renderDatasets() {
  const sel = $("ds-select");
  sel.replaceChildren(el("option", { value: "" }, "None (preview shows field names)"),
    ...datasets.map(d => el("option", { value: d.id, selected: d.id === tpl.dataset?.id }, `${d.name} · ${d.row_count} rows`)));
}
$("ds-select").addEventListener("change", e => { row = 1; save({ dataset_id: e.target.value || null }); });
$("ds-upload").addEventListener("click", () => $("ds-file").click());
$("ds-file").addEventListener("change", async e => {
  const file = e.target.files[0]; e.target.value = "";
  if (!file) return;
  const form = new FormData(); form.append("file", file);
  note("ds-note", "Uploading…");
  try {
    const ds = await api("/datasets", { method: "POST", body: form });
    datasets = await api("/datasets"); row = 1;
    if (await save({ dataset_id: ds.id })) note("ds-note", `Loaded ${ds.row_count} rows, ${ds.columns.length} columns.`, "ok");
  } catch (err) { note("ds-note", err.message, "error"); }
});

// ---------- 2 · field mapping ----------
function renderFields() {
  const cols = columns();
  $("fields").replaceChildren(...tpl.fields.map(f => {
    const sel = el("select", { "aria-label": `Column for ${f.label}` },
      el("option", { value: "" }, "Fixed value (as tagged)"),
      ...cols.map(c => el("option", { value: c, selected: tpl.mapping[f.label] === c }, c)));
    sel.addEventListener("change", () => save({ mapping: { ...tpl.mapping, [f.label]: sel.value || null } }));
    return el("div", { class: "fieldrow" },
      el("b", { title: f.label }, f.label, f.count > 1 ? el("span", { class: "badge neutral" }, `×${f.count}`) : null), sel);
  }));
}

// ---------- 3 · font & style ----------
function renderStyle() {
  const sel = $("font-select"), groups = {};
  for (const f of catalog.bundled) (groups[f.category] ||= []).push(f);
  const optgroup = (label, items) => el("optgroup", { label }, items.map(f => el("option", { value: f.key, selected: f.key === tpl.style.font }, f.label)));
  sel.replaceChildren(...Object.entries(groups).map(([c, items]) => optgroup(c, items)),
    ...(catalog.user.length ? [optgroup("Your fonts", catalog.user)] : []));
  document.querySelectorAll("[data-color]").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.color === tpl.style.color)));
  $("color-custom").value = tpl.style.color;
  document.querySelectorAll("#align [data-align]").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.align === tpl.style.align)));
  $("max-size").value = tpl.style.max_font_size;
}
$("font-select").addEventListener("change", e => save({ font: e.target.value }));
document.querySelectorAll("[data-color]").forEach(b => b.addEventListener("click", () => save({ color: b.dataset.color })));
$("color-custom").addEventListener("change", e => save({ color: e.target.value }));
document.querySelectorAll("#align [data-align]").forEach(b => b.addEventListener("click", () => save({ align: b.dataset.align })));
$("max-size").addEventListener("change", e => save({ max_font_size: Math.min(Math.max(parseInt(e.target.value, 10) || 0, 0), 200) }));

$("font-add").addEventListener("click", () => $("font-file").click());
$("font-file").addEventListener("change", async e => {
  const file = e.target.files[0]; e.target.value = "";
  if (!file) return;
  const form = new FormData(); form.append("file", file); form.append("name", file.name.replace(/\.[^.]+$/, ""));
  try {
    const f = await api("/fonts", { method: "POST", body: form });
    catalog = await api("/fonts/catalog");
    toast(`Added font “${f.name}”`, "ok");
    await save({ font: `user:${f.id}` });
  } catch (err) { fail(err); }
});

// ---------- 4 · generate ----------
function renderGenerate() {
  const n = rowCount(), cols = columns();
  const from = $("row-from"), to = $("row-to");
  from.max = to.max = n;
  if (!from.value || +from.value > n) from.value = 1;
  if (!to.value || +to.value > n || +to.value < 1) to.value = n || "";
  $("name-col").replaceChildren(el("option", { value: "" }, "Row number"),
    ...cols.map(c => el("option", { value: c, selected: tpl.filename_column === c }, c)));
  $("generate").disabled = !n;
  $("generate").textContent = n ? `Generate ${Math.max(+to.value - +from.value + 1, 0)} PDFs` : "Choose a data sheet first";
}
["row-from", "row-to"].forEach(id => $(id).addEventListener("input", renderGenerate));
$("name-col").addEventListener("change", e => save({ filename_column: e.target.value || null }));

$("generate").addEventListener("click", async () => {
  const body = { start: +$("row-from").value, end: +$("row-to").value, mode: $("mode").value };
  const btn = $("generate"); btn.disabled = true; note("gen-msg", "Generating…");
  try {
    const res = await DM.request(`${base}/fill`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (!res.ok) throw new Error(DM.errorText(await res.json().catch(() => null), res.status));
    if (body.mode === "merged") {
      const r = await res.json();
      note("gen-msg", `Saved “${r.document.filename}” to your documents (${r.rows} rows).`, "ok");
      warn(r.overflow, r.warnings.join("; "));
    } else {
      const blob = await res.blob();
      const name = /filename="([^"]+)"/.exec(res.headers.get("Content-Disposition") || "")?.[1] || "filled.zip";
      const a = Object.assign(document.createElement("a"), { href: URL.createObjectURL(blob), download: name });
      document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(a.href), 10000);
      note("gen-msg", `Downloaded ${res.headers.get("X-DocUMud-Rows")} PDFs.`, "ok");
      warn(+res.headers.get("X-DocUMud-Overflow"), res.headers.get("X-DocUMud-Warnings"));
    }
  } catch (e) { note("gen-msg", e.message, "error"); }
  renderGenerate();
});
function warn(overflow, text) {
  if (overflow) toast(`${overflow} value(s) were too long for their box and were shortened. Make the box bigger or lower the font size.`, "error");
  if (text) toast(text, "error");
}

// ---------- boot ----------
function renderAll() { renderDatasets(); renderFields(); renderStyle(); renderGenerate(); }

(async () => {
  try {
    [tpl, datasets, catalog] = await Promise.all([api(`${base}/template`), api("/datasets"), api("/fonts/catalog")]);
    $("title").textContent = `Fill from data · ${tpl.filename}`;
    $("edit-tags").href = $("go-tag").href = `/documents/${window.FILL.documentId}/tagger`;
    if (!tpl.fields.length) { $("no-tags").hidden = false; return; }
    $("work").hidden = false;
    renderAll(); refreshPreview();
  } catch (e) { fail(e); }
})();
