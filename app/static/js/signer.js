// Shared signing UI. Three signature sources (draw / type / upload) each yield black-ink-on-transparent
// pixels; compose() tints them with the chosen ink into #out, which is what gets placed and submitted.
const $ = id => document.getElementById(id);
const cfg = window.SIGN;
const isOwn = cfg.mode === "own";
const pub = path => `/signing/public/${cfg.token}${path}`;
const W = 600, H = 200;

const pad = $("pad"), padCtx = pad.getContext("2d");
const out = $("out"), outCtx = out.getContext("2d");
const src = Object.assign(document.createElement("canvas"), { width: W, height: H }); // typed / uploaded ink
const srcCtx = src.getContext("2d");

let tab = "draw", ink = "#111111", drawn = false, drawing = false, uploadReady = false;
let page = 0, pageCount = 1, info = null, verifyToken = null;
const vq = () => (verifyToken ? `?v=${encodeURIComponent(verifyToken)}` : "");
const place = { x: 0.55, y: 0.35, placed: false };

const setStatus = (text, cls = "") => { const s = $("status"); s.className = `msg ${cls}`; s.textContent = text; };

// ---------------- fonts ----------------
const fonts = ["Great Vibes", "Allura", "Sacramento", "Kristi", "Mr Dafoe", "Marck Script", "Herr Von Muellerhoff", "Dancing Script", "Caveat"]
  .map(f => ({ key: f, family: f, label: f, custom: false }));
let fontKey = fonts[0].key;
const currentFont = () => fonts.find(f => f.key === fontKey) || fonts[0];

function renderFontGrid() {
  const sample = $("typed").value.trim() || "Your name";
  $("font-grid").replaceChildren(...fonts.map(f => {
    const b = document.createElement("button");
    b.type = "button"; b.className = "font-opt"; b.setAttribute("aria-pressed", String(f.key === fontKey));
    b.style.fontFamily = `"${f.family}", cursive`; b.textContent = sample;
    const small = document.createElement("small"); small.textContent = f.label + (f.custom ? " · yours" : "");
    b.append(small);
    if (f.deletable) {
      const del = document.createElement("span"); del.className = "del btn sm"; del.textContent = "✕"; del.title = "Remove this font";
      del.addEventListener("click", e => { e.stopPropagation(); removeFont(f); });
      b.append(del);
    }
    b.addEventListener("click", () => { fontKey = f.key; renderFontGrid(); refresh(); });
    return b;
  }));
}

async function registerFont(buffer, label, extra = {}) {
  const family = `UF-${extra.id || Math.random().toString(36).slice(2, 10)}`;
  const face = new FontFace(family, buffer);
  await face.load();
  document.fonts.add(face);
  const font = { key: family, family, label, custom: true, ...extra };
  fonts.push(font);
  return font;
}

async function removeFont(f) {
  if (!confirm(`Remove font "${f.label}"?`)) return;
  try {
    await DM.api(`/fonts/${f.id}`, { method: "DELETE" });
    fonts.splice(fonts.indexOf(f), 1);
    if (fontKey === f.key) fontKey = fonts[0].key;
    renderFontGrid(); refresh();
  } catch (e) { DM.toast(e.message, "error"); }
}

$("font-add").addEventListener("click", () => $("font-file").click());
$("font-file").addEventListener("change", async e => {
  const file = e.target.files[0]; e.target.value = "";
  if (!file) return;
  const note = $("font-note");
  try {
    const buffer = await file.arrayBuffer();
    const label = file.name.replace(/\.[^.]+$/, "").slice(0, 60);
    let font;
    if (isOwn) {
      const form = new FormData(); form.append("file", new Blob([buffer]), file.name); form.append("name", label);
      const saved = await DM.api("/fonts", { method: "POST", body: form });
      font = await registerFont(buffer, saved.name, { id: saved.id, deletable: true });
      note.textContent = "Saved to your account.";
    } else {
      font = await registerFont(buffer, label);
      note.textContent = "Used on this device only; it is not uploaded.";
    }
    fontKey = font.key; renderFontGrid(); refresh();
  } catch (err) {
    note.textContent = err.name === "NetworkError" || !err.message ? "That font file could not be read." : err.message;
  }
});

async function loadSavedFonts() {
  const saved = await DM.api("/fonts");
  for (const s of saved) {
    try {
      const res = await DM.request(`/fonts/${s.id}/file`);
      if (res.ok) await registerFont(await res.arrayBuffer(), s.name, { id: s.id, deletable: true });
    } catch { /* skip an unreadable font */ }
  }
  renderFontGrid();
}

// ---------------- ink + compose ----------------
let refreshToken = 0, previewQueued = false;
function resetPad() { padCtx.clearRect(0, 0, W, H); padCtx.lineWidth = 3.5; padCtx.lineCap = "round"; padCtx.lineJoin = "round"; padCtx.strokeStyle = "#000"; drawn = false; }
resetPad();

function sourceCanvas() { return tab === "draw" ? pad : src; }
function compose() {
  outCtx.globalCompositeOperation = "source-over";
  outCtx.clearRect(0, 0, W, H);
  outCtx.drawImage(sourceCanvas(), 0, 0);
  outCtx.globalCompositeOperation = "source-in";
  outCtx.fillStyle = ink; outCtx.fillRect(0, 0, W, H);
  outCtx.globalCompositeOperation = "source-over";
}

const hasSignature = () => tab === "draw" ? drawn : tab === "type" ? $("typed").value.trim().length > 0 : uploadReady;

async function renderTyped() {
  srcCtx.clearRect(0, 0, W, H);
  const text = $("typed").value.trim();
  if (!text) return;
  const family = currentFont().family;
  await document.fonts.load(`80px "${family}"`, text);
  let size = 140, m;
  do {
    srcCtx.font = `${size}px "${family}", cursive`;
    m = srcCtx.measureText(text);
    size -= 4;
  } while (size > 16 && (m.width > W - 40 || m.actualBoundingBoxAscent + m.actualBoundingBoxDescent > H - 30));
  const left = m.actualBoundingBoxLeft, right = m.actualBoundingBoxRight;
  srcCtx.fillStyle = "#000"; srcCtx.textBaseline = "alphabetic";
  srcCtx.fillText(text, (W - (left + right)) / 2 + left, H / 2 + (m.actualBoundingBoxAscent - m.actualBoundingBoxDescent) / 2);
}

async function refresh() {
  const token = ++refreshToken;
  if (tab === "type") await renderTyped();
  if (token !== refreshToken) return;
  compose();
  const box = $("sigbox"), img = $("sigbox-img");
  img.src = hasSignature() ? out.toDataURL("image/png") : "";
  box.hidden = !(place.placed && hasSignature());
  positionBox();
}
const refreshSoon = () => { if (previewQueued) return; previewQueued = true; requestAnimationFrame(() => { previewQueued = false; refresh(); }); };

document.querySelectorAll(".swatch").forEach(b => b.addEventListener("click", () => {
  ink = b.dataset.ink;
  document.querySelectorAll(".swatch").forEach(x => x.setAttribute("aria-pressed", String(x === b)));
  refresh();
}));

document.querySelectorAll(".tabs button").forEach(b => b.addEventListener("click", () => {
  tab = b.dataset.tab;
  document.querySelectorAll(".tabs button").forEach(x => x.classList.toggle("active", x === b));
  ["draw", "type", "upload"].forEach(t => { $(`tab-${t}`).hidden = t !== tab; });
  if (tab === "type") renderFontGrid();
  refresh();
}));

// ---------------- draw ----------------
const padPos = e => { const r = pad.getBoundingClientRect(); return [(e.clientX - r.left) * W / r.width, (e.clientY - r.top) * H / r.height]; };
pad.addEventListener("pointerdown", e => { drawing = true; pad.setPointerCapture(e.pointerId); padCtx.beginPath(); padCtx.moveTo(...padPos(e)); });
pad.addEventListener("pointermove", e => { if (!drawing) return; padCtx.lineTo(...padPos(e)); padCtx.stroke(); drawn = true; refreshSoon(); });
["pointerup", "pointercancel"].forEach(t => pad.addEventListener(t, () => { drawing = false; refresh(); }));
$("pad-clear").addEventListener("click", () => { resetPad(); refresh(); });

// ---------------- type ----------------
$("typed").addEventListener("input", () => { renderFontGrid(); refresh(); });

// ---------------- upload / scan ----------------
let ratios = null, imgW = 0, imgH = 0;
const clamp = (v, lo, hi) => Math.min(Math.max(v, lo), hi);
const imgMsg = (t, cls = "") => { $("img-msg").className = `msg ${cls}`; $("img-msg").textContent = t; };

async function decodeImage(file) {
  try { return await createImageBitmap(file, { imageOrientation: "from-image" }); }
  catch {
    const url = URL.createObjectURL(file);
    try { const im = new Image(); im.src = url; await im.decode(); return im; } finally { URL.revokeObjectURL(url); }
  }
}

// Per-pixel ink "darkness" relative to the local paper colour, so shadows and uneven light don't count as ink.
function analyse(bmp) {
  const sw = bmp.width, sh = bmp.height, scale = Math.min(1, 1000 / Math.max(sw, sh));
  imgW = Math.max(1, Math.round(sw * scale)); imgH = Math.max(1, Math.round(sh * scale));
  const c = Object.assign(document.createElement("canvas"), { width: imgW, height: imgH });
  const cx = c.getContext("2d", { willReadFrequently: true });
  cx.drawImage(bmp, 0, 0, imgW, imgH);
  const px = cx.getImageData(0, 0, imgW, imgH).data;

  const small = Object.assign(document.createElement("canvas"), { width: Math.max(8, Math.round(imgW / 20)), height: Math.max(8, Math.round(imgH / 20)) });
  small.getContext("2d").drawImage(c, 0, 0, small.width, small.height);
  const bgc = Object.assign(document.createElement("canvas"), { width: imgW, height: imgH });
  const bx = bgc.getContext("2d", { willReadFrequently: true });
  bx.imageSmoothingEnabled = true; bx.imageSmoothingQuality = "high";
  bx.drawImage(small, 0, 0, imgW, imgH);
  const bp = bx.getImageData(0, 0, imgW, imgH).data;

  ratios = new Float32Array(imgW * imgH);
  for (let i = 0, p = 0; i < ratios.length; i++, p += 4) {
    const g = 0.299 * px[p] + 0.587 * px[p + 1] + 0.114 * px[p + 2];
    const b = 0.299 * bp[p] + 0.587 * bp[p + 1] + 0.114 * bp[p + 2];
    ratios[i] = b > 1 ? Math.min(g / b, 1) : 1;
  }
}

function buildUploadInk() {
  const lo = Number($("img-clean").value), span = 0.2;
  const mx = Math.round(imgW * 0.02), my = Math.round(imgH * 0.02); // ignore photo edges / table
  const alpha = new Uint8ClampedArray(imgW * imgH);
  const rows = new Uint32Array(imgH), cols = new Uint32Array(imgW);
  for (let y = my; y < imgH - my; y++) for (let x = mx; x < imgW - mx; x++) {
    const i = y * imgW + x, a = clamp((1 - ratios[i] - lo) / span, 0, 1);
    alpha[i] = a * 255;
    if (a > 0.5) { rows[y]++; cols[x]++; }
  }
  const first = arr => arr.findIndex(n => n >= 2), last = arr => { for (let i = arr.length - 1; i >= 0; i--) if (arr[i] >= 2) return i; return -1; };
  const y0 = first(rows), y1 = last(rows), x0 = first(cols), x1 = last(cols);
  if (y0 < 0 || x0 < 0 || y1 - y0 < 4 || x1 - x0 < 8) { uploadReady = false; srcCtx.clearRect(0, 0, W, H); imgMsg("No signature found. Use a dark pen on plain white paper, or lower “Remove faint marks”.", "error"); return; }
  const pad_ = 4, cx0 = Math.max(0, x0 - pad_), cy0 = Math.max(0, y0 - pad_), cw = Math.min(imgW, x1 + pad_) - cx0, ch = Math.min(imgH, y1 + pad_) - cy0;
  const data = new ImageData(cw, ch);
  for (let y = 0; y < ch; y++) for (let x = 0; x < cw; x++) data.data[(y * cw + x) * 4 + 3] = alpha[(cy0 + y) * imgW + cx0 + x];
  const tmp = Object.assign(document.createElement("canvas"), { width: cw, height: ch });
  tmp.getContext("2d").putImageData(data, 0, 0);
  const k = Math.min((W - 40) / cw, (H - 30) / ch, 3);
  srcCtx.clearRect(0, 0, W, H);
  srcCtx.imageSmoothingEnabled = true; srcCtx.imageSmoothingQuality = "high";
  srcCtx.drawImage(tmp, (W - cw * k) / 2, (H - ch * k) / 2, cw * k, ch * k);
  uploadReady = true; imgMsg("");
}

async function handleImage(file) {
  if (!file) return;
  if (!file.type.startsWith("image/")) return imgMsg("Please choose an image file.", "error");
  if (file.size > 20 * 1024 * 1024) return imgMsg("Image is too large (max 20 MB).", "error");
  try {
    imgMsg("Processing…");
    analyse(await decodeImage(file));
    $("img-clean").disabled = false;
    buildUploadInk(); refresh();
  } catch { uploadReady = false; imgMsg("That image could not be read.", "error"); refresh(); }
}
$("img-pick").addEventListener("click", () => $("img-file").click());
$("img-scan").addEventListener("click", () => $("img-cam").click());
["img-file", "img-cam"].forEach(id => $(id).addEventListener("change", e => { handleImage(e.target.files[0]); e.target.value = ""; }));
$("img-clean").addEventListener("input", () => { if (ratios) { buildUploadInk(); refresh(); } });

// ---------------- page placement ----------------
const wrapEl = $("pagewrap"), pageImg = $("page-img"), box = $("sigbox");
function positionBox() {
  const r = pageImg.getBoundingClientRect();
  const w = $("sig-size").value * r.width, h = w * H / W;
  Object.assign(box.style, { left: `${place.x * r.width}px`, top: `${place.y * r.height}px`, width: `${w}px`, height: `${h}px` });
}
function moveTo(e) {
  const r = pageImg.getBoundingClientRect();
  const w = Number($("sig-size").value), hFrac = w * (r.width * H / W) / r.height;
  place.x = clamp((e.clientX - r.left) / r.width - w / 2, 0, 1 - w);
  place.y = clamp((e.clientY - r.top) / r.height - hFrac / 2, 0, 1 - hFrac);
  place.placed = true; refresh();
}
let dragging = false;
wrapEl.addEventListener("pointerdown", e => { dragging = true; wrapEl.setPointerCapture(e.pointerId); moveTo(e); });
wrapEl.addEventListener("pointermove", e => { if (dragging) moveTo(e); });
["pointerup", "pointercancel"].forEach(t => wrapEl.addEventListener(t, () => { dragging = false; }));
$("sig-size").addEventListener("input", () => { if (place.placed) { const b = box.getBoundingClientRect(); moveTo({ clientX: b.left + b.width / 2, clientY: b.top + b.height / 2 }); } });
window.addEventListener("resize", positionBox);

let lastUrl = null;
async function loadPage() {
  $("page-label").textContent = `Page ${page + 1} of ${pageCount}`;
  $("prev").disabled = page === 0; $("next").disabled = page >= pageCount - 1;
  try {
    const url = isOwn ? await DM.blobUrl(`/documents/${cfg.documentId}/pages/${page}.png`) : pub(`/pages/${page}.png`) + vq();
    if (lastUrl) URL.revokeObjectURL(lastUrl);
    lastUrl = isOwn ? url : null;
    pageImg.src = url;
  } catch (e) { setStatus(e.message, "error"); }
}
pageImg.addEventListener("load", positionBox);
$("prev").addEventListener("click", () => { page--; loadPage(); });
$("next").addEventListener("click", () => { page++; loadPage(); });

// ---------------- submit ----------------
$("submit-signature").addEventListener("click", async () => {
  if (!hasSignature()) return setStatus("Add your signature first: draw, type or upload it.", "error");
  if (!place.placed) return setStatus("Click on the page to place your signature.", "error");
  const name = $("signer-name").value.trim();
  if (!name) return setStatus("Enter your full name.", "error");
  if (!isOwn && !$("consent").checked) return setStatus("Please confirm your consent to sign electronically.", "error");

  await refresh();
  const blob = await new Promise(r => out.toBlob(r, "image/png"));
  const form = new FormData();
  form.append("signature", blob, "signature.png");
  form.append("page_number", page);
  form.append("x", place.x); form.append("y", place.y);
  form.append("width", $("sig-size").value);
  form.append("signer_name", name);
  if (!isOwn) form.append("consent", "true");

  $("submit-signature").disabled = true; setStatus("Signing…");
  try {
    if (isOwn) {
      await DM.api(`/documents/${cfg.documentId}/sign`, { method: "POST", body: form });
      setStatus("Signed copy saved.", "ok");
      setTimeout(() => (location.href = "/dashboard"), 900);
      return;
    }
    const res = await fetch(pub("/sign"), { method: "POST", body: form, headers: verifyToken ? { "X-Verify": verifyToken } : {} });
    const body = await res.json().catch(() => null);
    if (!res.ok) throw new Error(DM.errorText(body, res.status));
    $("signer-area").hidden = true; $("intro").hidden = true; $("done").hidden = false;
    $("done-text").textContent = "Your signature has been recorded. You can close this page.";
    if (DM.token() && info?.mode === "in_person") { $("back").hidden = false; $("done-text").textContent = "Signature recorded. Please hand the device back."; }
  } catch (e) { setStatus(e.message, "error"); $("submit-signature").disabled = false; }
});

// ---------------- email-code verification (recipients) ----------------
const otpMsg = (t, cls = "") => { $("otp-msg").className = `msg ${cls}`; $("otp-msg").textContent = t; };
async function startPublic() {
  $("signer-area").hidden = false;
  $("open-pdf").href = pub("/document") + vq();
  fetch(pub("/viewed") + vq(), { method: "POST", headers: verifyToken ? { "X-Verify": verifyToken } : {} });
  await loadPage();
}
function showOtp() {
  $("otp-card").hidden = false;
  $("otp-text").textContent = `To protect this document, we'll email a 6-digit code to ${info.email_hint}.`;
}
async function otpCall(path, body) {
  const res = await fetch(pub(path), { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
  const data = await res.json().catch(() => null);
  if (!res.ok) throw new Error(DM.errorText(data, res.status));
  return data;
}
$("otp-send").addEventListener("click", async () => {
  $("otp-send").disabled = true; otpMsg("Sending…");
  try {
    const r = await otpCall("/otp/send");
    $("otp-entry").hidden = false; $("otp-code").focus();
    otpMsg(`Code sent to ${r.email_hint}. It expires in ${r.expires_minutes} minutes.`, "ok");
    $("otp-send").textContent = "Send a new code";
    setTimeout(() => { $("otp-send").disabled = false; }, 30000);
  } catch (e) { otpMsg(e.message, "error"); $("otp-send").disabled = false; }
});
$("otp-verify").addEventListener("click", async () => {
  const code = $("otp-code").value.replace(/\D/g, "");
  if (code.length !== 6) return otpMsg("Enter the 6-digit code from your email.", "error");
  $("otp-verify").disabled = true;
  try {
    verifyToken = (await otpCall("/otp/verify", { code })).verify_token;
    $("otp-card").hidden = true;
    await startPublic();
  } catch (e) { otpMsg(e.message, "error"); }
  $("otp-verify").disabled = false;
});
$("otp-code").addEventListener("keydown", e => { if (e.key === "Enter") $("otp-verify").click(); });

// ---------------- init ----------------
(async () => {
  renderFontGrid(); refresh();
  try {
    if (isOwn) {
      const [docs, me] = await Promise.all([DM.api("/documents"), DM.api("/auth/me")]);
      const d = docs.find(x => x.id === cfg.documentId);
      if (!d) throw new Error("Document not found.");
      pageCount = d.pages; $("signer-name").value = me.name || ""; $("typed").value = me.name || "";
      renderFontGrid();
      loadSavedFonts().catch(() => {});
    } else {
      const res = await fetch(pub("/info"));
      const body = await res.json().catch(() => null);
      if (!res.ok) throw new Error(DM.errorText(body, res.status));
      info = body; pageCount = info.pages;
      $("intro-text").textContent = `${info.sender} has asked you to review and sign “${info.filename}”.`;
      $("open-pdf").href = pub("/document");
      $("signer-name").value = info.recipient_name || ""; $("typed").value = info.recipient_name || "";
      renderFontGrid();
      if (info.otp_required) { showOtp(); return; }
      await startPublic();
      return;
    }
    await loadPage();
  } catch (e) {
    if (isOwn) return setStatus(e.message, "error");
    $("intro").hidden = true; $("signer-area").hidden = true; $("error").hidden = false;
    $("error-text").textContent = e.message;
  }
})();
