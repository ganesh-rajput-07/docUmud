const $ = id => document.getElementById(id);
const { api, el, toast, fmtDate } = DM;
const json = (method, body) => ({ method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
const SIG_FONTS = ["GreatVibes", "Allura", "Sacramento", "Kristi", "MrDafoe", "MarckScript", "HerrVonMuellerhoff", "DancingScript", "Caveat"];
let src = "upload";

async function load() {
  const { data } = await api("/v1/templates");
  const box = $("list"); box.replaceChildren();
  if (!data.length) return box.append(el("div", { class: "empty" }, el("b", {}, "No templates yet"), "Create one from a PDF: place the fields once, reuse it forever."));
  box.append(el("div", { class: "table-wrap" }, el("table", {},
    el("thead", {}, el("tr", {}, ["Name", "Roles", "Fields", "Envelopes", "Updated", ""].map(h => el("th", { class: h === "" ? "end" : "" }, h)))),
    el("tbody", {}, data.map(t => el("tr", {},
      el("td", { class: "name", title: t.name }, el("a", { href: `/templates/${t.id}/design` }, t.name)),
      el("td", { class: "muted" }, t.roles.map(r => r.label).join(", ")), el("td", {}, String(t.field_count)),
      el("td", {}, String(t.envelope_count)), el("td", {}, fmtDate(t.updated_at)),
      el("td", { class: "actions" },
        el("a", { class: "btn sm primary", href: `/envelopes/new?template=${t.id}` }, "Use"),
        el("a", { class: "btn sm", href: `/templates/${t.id}/design` }, "Design"),
        el("button", { class: "sm", onclick: async () => { try { const c = await api(`/v1/templates/${t.id}/duplicate`, { method: "POST" }); location.href = `/templates/${c.id}/design`; } catch (e) { toast(e.message, "error"); } } }, "Duplicate"),
        el("button", { class: "sm danger", onclick: async () => { if (!confirm(`Archive "${t.name}"? Envelopes already created are unaffected.`)) return; try { await api(`/v1/templates/${t.id}`, { method: "DELETE" }); toast("Template archived"); load(); } catch (e) { toast(e.message, "error"); } } }, "Archive"))))))));
}

// ---- new template ----
const dlg = $("new-dialog");
function openNew(docId) {
  $("new-form").reset(); $("nt-msg").textContent = ""; setSrc(docId ? "existing" : "upload");
  api("/documents").then(docs => { $("nt-doc").replaceChildren(...docs.map(d => el("option", { value: d.id, selected: d.id === docId }, d.filename)));
    if (docId) $("nt-name").value = (docs.find(d => d.id === docId)?.filename || "").replace(/\.pdf$/i, ""); });
  dlg.showModal();
}
function setSrc(s) { src = s; document.querySelectorAll("#new-form [data-src]").forEach(b => b.classList.toggle("active", b.dataset.src === s)); $("src-upload").hidden = s !== "upload"; $("src-existing").hidden = s !== "existing"; }
document.querySelectorAll("#new-form [data-src]").forEach(b => b.addEventListener("click", () => setSrc(b.dataset.src)));
$("new-btn").addEventListener("click", () => openNew());
$("nt-close").addEventListener("click", () => dlg.close());
$("nt-file").addEventListener("change", e => { if (!$("nt-name").value && e.target.files[0]) $("nt-name").value = e.target.files[0].name.replace(/\.pdf$/i, ""); });
$("new-form").addEventListener("submit", async e => {
  e.preventDefault(); const m = $("nt-msg"); $("nt-go").disabled = true; m.className = "msg"; m.textContent = "Creating…";
  try {
    let t;
    if (src === "upload") {
      const f = $("nt-file").files[0]; if (!f) throw new Error("Choose a PDF first.");
      const form = new FormData(); form.append("file", f); form.append("definition", JSON.stringify({ name: $("nt-name").value.trim() }));
      t = await api("/v1/templates/upload", { method: "POST", body: form });
    } else t = await api("/v1/templates", json("POST", { name: $("nt-name").value.trim(), document_id: $("nt-doc").value }));
    location.href = `/templates/${t.id}/design`;
  } catch (err) { m.className = "msg error"; m.textContent = err.message; $("nt-go").disabled = false; }
});

// ---- office signature ----
const sd = $("sig-dialog");
async function sigStatus() {
  const s = await api("/v1/signature-profile");
  $("sig-status").className = `msg ${s.has_signature ? "ok" : ""}`; $("sig-status").textContent = s.has_signature ? "A signature is saved." : "No signature saved yet.";
  $("sig-remove").hidden = !s.has_signature; const img = $("sig-img");
  if (s.has_signature) { img.src = await DM.blobUrl("/v1/signature-profile/signature.png"); img.style.display = ""; } else img.style.display = "none";
}
$("sig-font").replaceChildren(...SIG_FONTS.map(f => el("option", { value: f, style: `font-family:'${f.replace(/([a-z])([A-Z])/g, "$1 $2")}'` }, f.replace(/([a-z])([A-Z])/g, "$1 $2"))));
$("sig-btn").addEventListener("click", () => { $("sig-msg").textContent = ""; sd.showModal(); sigStatus().catch(e => toast(e.message, "error")); });
$("sig-close").addEventListener("click", () => sd.close());
$("sig-save").addEventListener("click", async () => {
  const form = new FormData(), file = $("sig-file").files[0], name = $("sig-name").value.trim();
  if (!file && !name) { $("sig-msg").className = "msg error"; $("sig-msg").textContent = "Type your name or choose a PNG."; return; }
  if (file) form.append("signature", file); else { form.append("typed_name", name); form.append("font", $("sig-font").value); }
  try { await api("/v1/signature-profile", { method: "PUT", body: form }); $("sig-msg").textContent = ""; toast("Signature saved", "ok"); await sigStatus(); }
  catch (e) { $("sig-msg").className = "msg error"; $("sig-msg").textContent = e.message; }
});
$("sig-remove").addEventListener("click", async () => { if (!confirm("Remove the saved signature?")) return; await api("/v1/signature-profile", { method: "DELETE" }); await sigStatus(); });

load().catch(e => toast(e.message, "error"));
const from = new URLSearchParams(location.search).get("new");
if (from) openNew(from);
