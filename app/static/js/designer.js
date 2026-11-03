// Template designer: pick a field type, draw it on the page, tune it in the properties panel. Every change saves immediately.
const $ = id => document.getElementById(id);
const { api, el, toast } = DM;
const base = `/v1/templates/${window.DESIGN.id}`;
const json = (method, body) => ({ method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
const COLORS = ["#4f46e5", "#0d9488", "#d97706", "#db2777", "#2563eb", "#7c3aed"];
const DATE_FORMATS = [["%d %b %Y", "05 Mar 2026"], ["%d/%m/%Y", "05/03/2026"], ["%d-%m-%Y", "05-03-2026"], ["%Y-%m-%d", "2026-03-05"], ["%B %d, %Y", "March 05, 2026"], ["%m/%d/%Y", "03/05/2026"]];
const clamp = (v, lo, hi) => Math.min(Math.max(v, lo), hi);

let tpl, types = [], fonts = null, page = 1, tool = null, selected = null, previewing = false, lastRadio = "";
let imgUrl = null, saveTimers = {}, pending = {};

const typeOf = id => types.find(t => t.id === id);
const roleIndex = id => Math.max(tpl.roles.findIndex(r => r.id === id), 0);
const colorOf = f => COLORS[roleIndex(f.role) % COLORS.length];
const fieldsOnPage = () => tpl.fields.filter(f => f.page === page);
const sel = () => tpl.fields.find(f => f.id === selected);
const fail = e => toast(e.message, "error");

// ---------------- palette ----------------
let placeRole = null;
function renderPalette() {
  const cats = [...new Set(types.map(t => t.category))];
  placeRole = placeRole && tpl.roles.some(r => r.id === placeRole) ? placeRole : tpl.roles[0]?.id;
  const roleSel = el("select", { "aria-label": "Place fields for role", onchange: e => { placeRole = e.target.value; } },
    tpl.roles.map(r => el("option", { value: r.id, selected: r.id === placeRole }, `${r.label}${r.kind === "office" ? " (you)" : ""}`)));
  $("palette").replaceChildren(el("h3", {}, "Place fields for"), roleSel,
    ...cats.flatMap(c => [el("h3", {}, c), ...types.filter(t => t.category === c).map(t =>
      el("button", { class: "tool", "aria-pressed": String(tool === t.id), title: t.description, onclick: () => { tool = tool === t.id ? null : t.id; renderPalette(); updateHint(); } }, t.label))]));
  $("pagewrap").classList.toggle("tool-on", !!tool);
}
function updateHint() { $("hint").textContent = previewing ? "Showing sample data" : tool ? `Drag on the page to place a ${typeOf(tool).label.toLowerCase()} (or just click).` : "Pick a field type on the left, then draw on the page."; }

// ---------------- page + boxes ----------------
async function loadPage() {
  $("page-label").textContent = `Page ${page} of ${tpl.pages}`; $("prev").disabled = page <= 1; $("next").disabled = page >= tpl.pages;
  try {
    const url = await DM.blobUrl(previewing ? `${base}/preview.png?page=${page}` : `${base}/page/${page}.png`);
    if (imgUrl) URL.revokeObjectURL(imgUrl); imgUrl = url; $("page-img").src = url;
  } catch (e) { fail(e); }
  renderBoxes();
}
function renderBoxes() {
  $("boxes").replaceChildren(...(previewing ? [] : fieldsOnPage().map(f => {
    const grip = el("div", { class: "grip" });
    const box = el("div", { class: `fbox${f.id === selected ? " sel" : ""}`, style: `--c:${colorOf(f)};left:${f.x * 100}%;top:${f.y * 100}%;width:${f.w * 100}%;height:${f.h * 100}%`, "data-id": f.id, title: `${f.label || f.name} · ${typeOf(f.type)?.label}` },
      el("span", {}, `${f.label || DM.humanize(f.name)}${f.type === "radio" ? ` = ${f.option}` : ""}`), f.id === selected ? grip : null);
    box.addEventListener("pointerdown", e => {
      e.stopPropagation();
      const mode = e.target === grip ? "resize" : "move";
      if (selected !== f.id) select(f.id);                  // re-renders the boxes, so grab the live element afterwards
      const live = $("boxes").querySelector(`[data-id="${f.id}"]`);
      if (live) beginDrag(e, f, mode, live);
    });
    return box;
  })));
}
$("prev").addEventListener("click", () => { page--; loadPage(); }); $("next").addEventListener("click", () => { page++; loadPage(); });

// ---- drag (move / resize) ----
function beginDrag(e, f, mode, target) {
  const r = $("page-img").getBoundingClientRect(), start = { x: e.clientX, y: e.clientY, fx: f.x, fy: f.y, fw: f.w, fh: f.h };
  try { target.setPointerCapture(e.pointerId); } catch { /* pointer already released */ }
  const move = ev => {
    const dx = (ev.clientX - start.x) / r.width, dy = (ev.clientY - start.y) / r.height;
    if (mode === "move") { f.x = clamp(start.fx + dx, 0, 1 - f.w); f.y = clamp(start.fy + dy, 0, 1 - f.h); }
    else { f.w = clamp(start.fw + dx, 0.01, 1 - f.x); f.h = clamp(start.fh + dy, 0.008, 1 - f.y); }
    Object.assign(target.style, { left: `${f.x * 100}%`, top: `${f.y * 100}%`, width: `${f.w * 100}%`, height: `${f.h * 100}%` });
  };
  const up = () => { target.removeEventListener("pointermove", move); target.removeEventListener("pointerup", up);
    if (f.x !== start.fx || f.y !== start.fy || f.w !== start.fw || f.h !== start.fh) patch(f, { x: f.x, y: f.y, w: f.w, h: f.h }); };
  target.addEventListener("pointermove", move); target.addEventListener("pointerup", up);
}

// ---- draw a new field ----
const wrap = $("pagewrap"), draft = $("draft");
let drawing = null;
const point = e => { const r = $("page-img").getBoundingClientRect(); return { x: clamp((e.clientX - r.left) / r.width, 0, 1), y: clamp((e.clientY - r.top) / r.height, 0, 1) }; };
wrap.addEventListener("pointerdown", e => { if (previewing) return; if (!tool) { select(null); return; } drawing = point(e); wrap.setPointerCapture(e.pointerId); });
wrap.addEventListener("pointermove", e => {
  if (!drawing) return; const p = point(e), x = Math.min(p.x, drawing.x), y = Math.min(p.y, drawing.y);
  Object.assign(draft.style, { left: `${x * 100}%`, top: `${y * 100}%`, width: `${Math.abs(p.x - drawing.x) * 100}%`, height: `${Math.abs(p.y - drawing.y) * 100}%` }); draft.hidden = false;
});
wrap.addEventListener("pointerup", async e => {
  if (!drawing) return; const p = point(e), t = typeOf(tool); const from = drawing; drawing = null; draft.hidden = true;
  let x = Math.min(p.x, from.x), y = Math.min(p.y, from.y), w = Math.abs(p.x - from.x), h = Math.abs(p.y - from.y);
  if (w < 0.01 || h < 0.006) { w = t.default_width; h = t.default_height; x = clamp(from.x - w / 2, 0, 1 - w); y = clamp(from.y - h / 2, 0, 1 - h); }  // a click: use the default size
  const body = { type: tool, page, x, y, w, h, role: placeRole };
  if (tool === "radio") {
    const name = prompt("Radio group name (boxes sharing a name form one choice):", lastRadio || "choice"); if (!name) return;
    const option = prompt("What does this box represent? (e.g. Yes)"); if (!option) return;
    Object.assign(body, { name: name.trim().replace(/\W+/g, "_"), option: option.trim(), label: name }); lastRadio = body.name;
  }
  if (tool === "dropdown") { const o = prompt("Options, separated by commas:", "Option 1, Option 2"); if (!o) return; body.options = o.split(",").map(s => s.trim()).filter(Boolean); }
  try { const f = await api(`${base}/fields`, json("POST", body)); tpl.fields.push(f); tool = null; renderPalette(); updateHint(); select(f.id); renderBoxes(); }
  catch (err) { fail(err); }
});

// ---------------- properties ----------------
function select(id) { selected = id; renderBoxes(); renderProps(); }
function patch(f, body, quiet) {
  Object.assign(f, body);
  pending[f.id] = { ...pending[f.id], ...body };          // several quick edits are sent together, none is lost
  clearTimeout(saveTimers[f.id]);
  saveTimers[f.id] = setTimeout(async () => {
    const sent = pending[f.id]; delete pending[f.id];
    try { Object.assign(f, await api(`${base}/fields/${f.id}`, json("PATCH", sent))); renderBoxes(); renderFieldList(); }
    catch (e) { fail(e); try { Object.assign(f, (await api(base)).fields.find(x => x.id === f.id)); } catch {} renderProps(); renderBoxes(); }
  }, 250);
}
const row = (label, input) => el("div", {}, el("label", {}, label), input);
const text = (f, key, opts = {}) => { const i = el("input", { type: "text", value: f[key] ?? "", placeholder: opts.placeholder || "" }); i.addEventListener("change", () => patch(f, { [key]: i.value.trim() }, true)); return i; };
function styleInput(f, key, kind = "text", extra = {}) {
  const v = f.style[key] ?? ""; let i;
  if (kind === "select") { i = el("select", {}, extra.options.map(([val, lab]) => el("option", { value: val, selected: String(v) === String(val) }, lab))); }
  else i = el("input", { type: kind, value: kind === "color" ? (v || "#111111") : v, min: extra.min, max: extra.max, placeholder: extra.placeholder || "" });
  if (kind === "checkbox") { i.checked = !!v; i.addEventListener("change", () => setStyle(f, key, i.checked || "")); }
  else i.addEventListener("change", () => setStyle(f, key, kind === "number" ? (i.value === "" ? "" : +i.value) : i.value));
  return i;
}
function setStyle(f, key, value) { const s = { ...f.style }; if (value === "" || value == null || value === false) delete s[key]; else s[key] = value; patch(f, { style: s }, true); }
function setValidation(f, key, value) { const v = { ...f.validation }; if (value === "" || value == null) delete v[key]; else v[key] = value; patch(f, { validation: v }, true); }

function renderProps() {
  const f = sel(), box = $("props");
  if (!f) { box.replaceChildren(el("h3", {}, "Field settings"), el("p", { class: "empty-note" }, "Select a field on the page to edit it."), el("h3", {}, "All fields"), fieldListEl()); return; }
  const t = typeOf(f.type), t2 = f.type;
  const roleSel = el("select", {}, tpl.roles.map(r => el("option", { value: r.id, selected: r.id === f.role }, r.label))); roleSel.addEventListener("change", () => { patch(f, { role: roleSel.value }); renderBoxes(); });
  const typeSel = el("select", {}, types.map(x => el("option", { value: x.id, selected: x.id === f.type }, x.label))); typeSel.addEventListener("change", () => { patch(f, { type: typeSel.value }); setTimeout(renderProps, 400); });
  const req = el("input", { type: "checkbox" }); req.checked = f.required; req.addEventListener("change", () => patch(f, { required: req.checked }, true));
  const opts = el("textarea", { rows: 3 }, (f.options || []).join("\n")); opts.addEventListener("change", () => patch(f, { options: opts.value.split("\n").map(s => s.trim()).filter(Boolean) }, true));
  const aliases = el("input", { type: "text", value: (f.aliases || []).join(", "), placeholder: "other_data_key, another" }); aliases.addEventListener("change", () => patch(f, { aliases: aliases.value.split(",").map(s => s.trim()).filter(Boolean) }, true));
  const textual = ["text", "description", "address", "full_name", "first_name", "last_name", "company", "job_title", "id_number", "email", "phone"].includes(t2);
  const fontOpts = [["", "Template default"], ...(fonts ? [...fonts.bundled.map(x => [x.key, x.label]), ...fonts.user.map(x => [x.key, x.label + " (yours)"])] : [])];
  const parts = [
    el("h3", {}, `${t.label} field`),
    row("Label (shown to people)", text(f, "label")),
    row("Key (used in API data)", text(f, "name")),
    el("div", { class: "two" }, row("Type", typeSel), row("Role", roleSel)),
    el("label", { class: "check-row", style: "margin:10px 0" }, req, el("span", {}, "Required")),
  ];
  if (t.has_options) parts.push(row(t2 === "radio" ? "Group choices (shared)" : "Options (one per line)", opts));
  if (t2 === "radio") parts.push(row("This box means", text(f, "option")));
  if (t.kind === "text") parts.push(row("Default value", text(f, "default_value")));
  parts.push(row("Also fill from these data keys", aliases));
  if (t.kind === "text" || t2 === "checkbox" || t2 === "radio") {
    parts.push(el("h3", {}, "Style"));
    if (t.kind === "text") parts.push(el("div", { class: "two" }, row("Font", styleInput(f, "font", "select", { options: fontOpts })), row("Size (0 = fit)", styleInput(f, "font_size", "number", { min: 0, max: 200, placeholder: "auto" }))),
      el("div", { class: "two" }, row("Colour", styleInput(f, "color", "color")), row("Align", styleInput(f, "align", "select", { options: [["", "Left"], ["center", "Center"], ["right", "Right"]] }))));
    if (t2 === "date" || t2 === "date_signed") parts.push(row("Date format", styleInput(f, "date_format", "select", { options: [["", "05 Mar 2026"], ...DATE_FORMATS.slice(1)] })));
    if (t2 === "amount") parts.push(el("div", { class: "two" }, row("Currency", styleInput(f, "currency", "text", { placeholder: "₹ $ €" })), row("Decimals", styleInput(f, "decimals", "number", { min: 0, max: 6, placeholder: "2" }))),
      row("Grouping", styleInput(f, "grouping", "select", { options: [["", "1,234,567 (international)"], ["indian", "12,34,567 (Indian)"], ["none", "No separators"]] })));
    if (t2 === "number") parts.push(el("div", { class: "two" }, row("Decimals", styleInput(f, "decimals", "number", { min: 0, max: 6 })), row("Grouping", styleInput(f, "grouping", "select", { options: [["", "None"], ["intl", "1,234,567"], ["indian", "12,34,567"]] }))));
    if (t2 === "checkbox" || t2 === "radio") parts.push(row("Mark", styleInput(f, "mark", "select", { options: [["", t2 === "radio" ? "Dot" : "Check"], ["check", "Check"], ["cross", "Cross"], ["dot", "Dot"]] })), row("Colour", styleInput(f, "color", "color")));
    if (textual) parts.push(el("label", { class: "check-row", style: "margin:8px 0" }, styleInput(f, "uppercase", "checkbox"), el("span", {}, "UPPERCASE")));
  }
  if (t.kind === "text" && !["checkbox", "date", "date_signed"].includes(t2)) {
    parts.push(el("h3", {}, "Validation"), row("Must match pattern (regex)", el("input", { type: "text", value: f.validation.pattern || "", placeholder: "[A-Z]{5}[0-9]{4}[A-Z]", onchange: e => setValidation(f, "pattern", e.target.value.trim()) })),
      el("div", { class: "two" }, row("Min length", el("input", { type: "number", min: 0, value: f.validation.min_length ?? "", onchange: e => setValidation(f, "min_length", e.target.value === "" ? "" : +e.target.value) })),
        row("Max length", el("input", { type: "number", min: 0, value: f.validation.max_length ?? "", onchange: e => setValidation(f, "max_length", e.target.value === "" ? "" : +e.target.value) }))));
  }
  parts.push(el("div", { class: "row", style: "margin-top:14px" }, el("button", { class: "sm danger", onclick: () => removeField(f) }, "Delete field")),
    el("h3", {}, "All fields"), fieldListEl());
  box.replaceChildren(...parts);
}
function fieldListEl() { return el("div", { class: "fieldlist", id: "fieldlist" }, tpl.fields.map(f => el("button", { class: f.id === selected ? "sel" : "", onclick: () => { page = f.page; loadPage().then(() => select(f.id)); } }, `p${f.page} · ${f.label || DM.humanize(f.name)} (${typeOf(f.type)?.label}${f.type === "radio" ? `: ${f.option}` : ""})`))); }
function renderFieldList() { const l = $("fieldlist"); if (l) l.replaceWith(fieldListEl()); }
async function removeField(f) { try { await api(`${base}/fields/${f.id}`, { method: "DELETE" }); tpl.fields = tpl.fields.filter(x => x.id !== f.id); select(null); } catch (e) { fail(e); } }

document.addEventListener("keydown", e => {
  const f = sel(); if (!f || /input|textarea|select/i.test(document.activeElement?.tagName)) return;
  if (e.key === "Delete" || e.key === "Backspace") { e.preventDefault(); removeField(f); return; }
  const step = e.shiftKey ? 0.01 : 0.002, d = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] }[e.key];
  if (d) { e.preventDefault(); f.x = clamp(f.x + d[0], 0, 1 - f.w); f.y = clamp(f.y + d[1], 0, 1 - f.h); patch(f, { x: f.x, y: f.y }, true); renderBoxes(); }
});

// ---------------- toolbar ----------------
$("preview-btn").addEventListener("click", () => { previewing = !previewing; $("preview-btn").textContent = previewing ? "Back to designing" : "Preview with sample data"; updateHint(); loadPage(); });
$("tpl-name").addEventListener("change", async e => { try { tpl = { ...tpl, ...(await api(base, json("PATCH", { name: e.target.value.trim() }))) }; toast("Renamed", "ok"); } catch (err) { fail(err); } });
$("json-btn").addEventListener("click", async () => {
  const t = await api(base), strip = f => { const { id, ...rest } = f; return rest; };
  const def = { name: t.name, description: t.description, roles: t.roles, defaults: t.defaults, fields: t.fields.map(strip) };
  try { await navigator.clipboard.writeText(JSON.stringify(def, null, 2)); toast("Template JSON copied: paste it into PUT /v1/templates/{id}", "ok"); } catch { toast("Copy failed", "error"); }
});

// ---- roles ----
function renderRoles(rows) {
  $("roles-body").replaceChildren(...rows.map((r, i) => el("div", { class: "rolerow" },
    el("input", { type: "text", value: r.label, "aria-label": "Role name", oninput: e => { r.label = e.target.value; } }),
    el("select", { "aria-label": "Kind", onchange: e => { r.kind = e.target.value; } }, [["signer", "External signer"], ["office", "Office (you)"]].map(([v, l]) => el("option", { value: v, selected: r.kind === v }, l))),
    el("input", { type: "number", min: 1, value: r.order, "aria-label": "Order", oninput: e => { r.order = +e.target.value || 1; } }),
    el("button", { class: "sm danger", "aria-label": "Remove role", onclick: () => { if (tpl.fields.some(f => f.role === r.id)) { $("roles-msg").className = "msg error"; $("roles-msg").textContent = `Move or delete the fields using "${r.label}" first.`; return; } rows.splice(i, 1); renderRoles(rows); } }, "✕"))));
}
let roleRows = [];
$("roles-btn").addEventListener("click", () => { roleRows = tpl.roles.map(r => ({ ...r })); $("roles-msg").textContent = ""; renderRoles(roleRows); $("roles-dialog").showModal(); });
$("role-add").addEventListener("click", () => { const n = roleRows.length + 1; roleRows.push({ id: `role${n}`, label: `Signer ${n}`, kind: "signer", order: n }); renderRoles(roleRows); });
$("roles-close").addEventListener("click", () => $("roles-dialog").close());
$("roles-save").addEventListener("click", async () => {
  const used = new Set();
  const roles = roleRows.map(r => { let id = r.id.startsWith("role") && !tpl.roles.some(x => x.id === r.id) ? r.label.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "") || r.id : r.id; if (!/^[a-z]/.test(id)) id = "r_" + id; while (used.has(id)) id += "_2"; used.add(id); return { id, label: r.label.trim() || id, kind: r.kind, order: r.order }; });
  try { tpl = { ...tpl, ...(await api(base, json("PATCH", { roles }))) }; $("roles-dialog").close(); renderPalette(); renderBoxes(); renderProps(); toast("Roles saved", "ok"); }
  catch (e) { $("roles-msg").className = "msg error"; $("roles-msg").textContent = e.message; }
});

// ---- defaults ----
$("defaults-btn").addEventListener("click", () => {
  const d = tpl.defaults || {};
  $("d-font").replaceChildren(el("option", { value: "" }, "Lato (built-in default)"), ...(fonts ? [...fonts.bundled.map(x => [x.key, x.label]), ...fonts.user.map(x => [x.key, x.label + " (yours)"])] : []).map(([v, l]) => el("option", { value: v, selected: d.font === v }, l)));
  $("d-size").value = d.font_size ?? 0; $("d-color").value = d.color || "#111111"; $("defaults-dialog").showModal();
});
$("d-close").addEventListener("click", () => $("defaults-dialog").close());
$("d-save").addEventListener("click", async () => {
  const defaults = { font: $("d-font").value || undefined, font_size: +$("d-size").value || undefined, color: $("d-color").value };
  try { tpl = { ...tpl, ...(await api(base, json("PATCH", { defaults }))) }; $("defaults-dialog").close(); toast("Default style saved", "ok"); } catch (e) { fail(e); }
});

// ---------------- boot ----------------
(async () => {
  try {
    [tpl, { types }, fonts] = await Promise.all([api(base), api("/v1/field-types"), api("/fonts/catalog")]);
    $("tpl-name").value = tpl.name; $("use-btn").href = `/envelopes/new?template=${tpl.id}`;
    renderPalette(); updateHint(); renderProps(); await loadPage();
  } catch (e) { fail(e); }
})();
