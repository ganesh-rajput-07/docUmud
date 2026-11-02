// Signature capture modal: draw, type (bundled script fonts) or upload a photo of a pen signature.
// SigPad.open({ title, text }) resolves to a transparent PNG Blob, or null if cancelled.
const SigPad = (() => {
  const FONTS = ["Great Vibes", "Allura", "Sacramento", "Kristi", "Mr Dafoe", "Marck Script", "Herr Von Muellerhoff", "Dancing Script", "Caveat"];
  const W = 600, H = 200, clamp = (v, lo, hi) => Math.min(Math.max(v, lo), hi);
  const mk = (w = W, h = H) => Object.assign(document.createElement("canvas"), { width: w, height: h });
  const el = DM.el;

  function open({ title = "Sign", text = "" } = {}) {
    return new Promise(resolve => {
      const pad = mk(), pc = pad.getContext("2d"), src = mk(), sc = src.getContext("2d"), out = mk(), oc = out.getContext("2d");
      let tab = "draw", ink = "#111111", drawn = false, drawing = false, upReady = false, font = FONTS[0], ratios = null, iw = 0, ih = 0;
      pc.lineWidth = 3.5; pc.lineCap = pc.lineJoin = "round"; pc.strokeStyle = "#000";

      const compose = async () => {
        if (tab === "type") await renderTyped();
        oc.globalCompositeOperation = "source-over"; oc.clearRect(0, 0, W, H); oc.drawImage(tab === "draw" ? pad : src, 0, 0);
        oc.globalCompositeOperation = "source-in"; oc.fillStyle = ink; oc.fillRect(0, 0, W, H); oc.globalCompositeOperation = "source-over";
        apply.disabled = !has(); preview.src = out.toDataURL();
      };
      const has = () => tab === "draw" ? drawn : tab === "type" ? typed.value.trim().length > 0 : upReady;
      async function renderTyped() {
        sc.clearRect(0, 0, W, H); const t = typed.value.trim(); if (!t) return;
        await document.fonts.load(`80px "${font}"`, t);
        let size = 140, m; do { sc.font = `${size}px "${font}", cursive`; m = sc.measureText(t); size -= 4; } while (size > 16 && (m.width > W - 40 || m.actualBoundingBoxAscent + m.actualBoundingBoxDescent > H - 30));
        sc.fillStyle = "#000"; sc.fillText(t, (W - (m.actualBoundingBoxLeft + m.actualBoundingBoxRight)) / 2 + m.actualBoundingBoxLeft, H / 2 + (m.actualBoundingBoxAscent - m.actualBoundingBoxDescent) / 2);
      }
      // ---- upload: remove paper/shadows, keep the pen strokes ----
      async function loadImage(file) {
        const bmp = await createImageBitmap(file, { imageOrientation: "from-image" }).catch(async () => { const im = new Image(); im.src = URL.createObjectURL(file); await im.decode(); return im; });
        const s = Math.min(1, 1000 / Math.max(bmp.width, bmp.height)); iw = Math.round(bmp.width * s); ih = Math.round(bmp.height * s);
        const c = mk(iw, ih), cx = c.getContext("2d", { willReadFrequently: true }); cx.drawImage(bmp, 0, 0, iw, ih);
        const px = cx.getImageData(0, 0, iw, ih).data, small = mk(Math.max(8, Math.round(iw / 20)), Math.max(8, Math.round(ih / 20))); small.getContext("2d").drawImage(c, 0, 0, small.width, small.height);
        const bgc = mk(iw, ih), bx = bgc.getContext("2d", { willReadFrequently: true }); bx.imageSmoothingQuality = "high"; bx.drawImage(small, 0, 0, iw, ih);
        const bp = bx.getImageData(0, 0, iw, ih).data; ratios = new Float32Array(iw * ih);
        for (let i = 0, p = 0; i < ratios.length; i++, p += 4) { const g = .299 * px[p] + .587 * px[p + 1] + .114 * px[p + 2], b = .299 * bp[p] + .587 * bp[p + 1] + .114 * bp[p + 2]; ratios[i] = b > 1 ? Math.min(g / b, 1) : 1; }
        buildInk();
      }
      function buildInk() {
        const lo = +clean.value, mx = Math.round(iw * .02), my = Math.round(ih * .02), alpha = new Uint8ClampedArray(iw * ih), rows = new Uint32Array(ih), cols = new Uint32Array(iw);
        for (let y = my; y < ih - my; y++) for (let x = mx; x < iw - mx; x++) { const i = y * iw + x, a = clamp((1 - ratios[i] - lo) / .2, 0, 1); alpha[i] = a * 255; if (a > .5) { rows[y]++; cols[x]++; } }
        const first = a => a.findIndex(n => n >= 2), last = a => { for (let i = a.length - 1; i >= 0; i--) if (a[i] >= 2) return i; return -1; };
        const y0 = first(rows), y1 = last(rows), x0 = first(cols), x1 = last(cols);
        if (y0 < 0 || x0 < 0 || y1 - y0 < 4 || x1 - x0 < 8) { upReady = false; sc.clearRect(0, 0, W, H); note.textContent = "No signature found. Use a dark pen on plain white paper."; return compose(); }
        const cw = Math.min(iw, x1 + 4) - Math.max(0, x0 - 4), ch = Math.min(ih, y1 + 4) - Math.max(0, y0 - 4), cx0 = Math.max(0, x0 - 4), cy0 = Math.max(0, y0 - 4);
        const data = new ImageData(cw, ch); for (let y = 0; y < ch; y++) for (let x = 0; x < cw; x++) data.data[(y * cw + x) * 4 + 3] = alpha[(cy0 + y) * iw + cx0 + x];
        const tmp = mk(cw, ch); tmp.getContext("2d").putImageData(data, 0, 0); const k = Math.min((W - 40) / cw, (H - 30) / ch, 3);
        sc.clearRect(0, 0, W, H); sc.imageSmoothingQuality = "high"; sc.drawImage(tmp, (W - cw * k) / 2, (H - ch * k) / 2, cw * k, ch * k); upReady = true; note.textContent = ""; compose();
      }

      // ---- UI ----
      const typed = el("input", { type: "text", maxlength: 60, value: text, placeholder: "Type your name", "aria-label": "Type your name" });
      const fontBtns = FONTS.map(f => el("button", { type: "button", class: "font-opt", style: `font-family:'${f}',cursive`, "aria-pressed": String(f === font), onclick: () => { font = f; fontBtns.forEach(b => b.setAttribute("aria-pressed", String(b === fontBtns[FONTS.indexOf(f)]))); compose(); } }, text || "Your name"));
      typed.addEventListener("input", () => { fontBtns.forEach(b => { b.firstChild.textContent = typed.value || "Your name"; }); compose(); });
      const clean = el("input", { type: "range", min: .05, max: .4, step: .01, value: .15, style: "width:160px", "aria-label": "Remove faint marks", oninput: () => ratios && buildInk() });
      const note = el("p", { class: "msg error" }), preview = el("img", { alt: "Signature preview", style: "max-width:100%;height:80px;display:block" });
      const file = el("input", { type: "file", accept: "image/*", hidden: true, onchange: e => e.target.files[0] && loadImage(e.target.files[0]).catch(() => { note.textContent = "That image could not be read."; }) });
      const cam = el("input", { type: "file", accept: "image/*", capture: "environment", hidden: true, onchange: e => e.target.files[0] && loadImage(e.target.files[0]).catch(() => { note.textContent = "That image could not be read."; }) });
      const panels = {
        draw: el("div", {}, pad, el("div", {}, el("button", { type: "button", class: "sm", onclick: () => { pc.clearRect(0, 0, W, H); drawn = false; compose(); } }, "Clear"))),
        type: el("div", {}, typed, el("div", { class: "font-grid", style: "margin-top:10px;max-height:190px" }, fontBtns)),
        upload: el("div", {}, el("p", { class: "muted", style: "margin-top:0" }, "Sign with a dark pen on plain white paper, then upload or photograph it."),
          el("div", { class: "row" }, el("button", { type: "button", onclick: () => file.click() }, "Upload image"), el("button", { type: "button", onclick: () => cam.click() }, "Take photo"), file, cam),
          el("div", { class: "row", style: "margin-top:8px" }, el("span", { class: "muted" }, "Remove faint marks"), clean), note),
      };
      const tabs = Object.keys(panels).map(k => el("button", { type: "button", class: k === tab ? "active" : "", onclick: () => { tab = k; tabs.forEach((b, i) => b.classList.toggle("active", Object.keys(panels)[i] === k)); Object.entries(panels).forEach(([n, p]) => { p.hidden = n !== k; }); compose(); } }, { draw: "Draw", type: "Type", upload: "Upload / scan" }[k]));
      Object.entries(panels).forEach(([n, p]) => { p.hidden = n !== tab; });
      const swatches = ["#111111", "#1d4ed8", "#7f1d1d"].map(c => el("button", { type: "button", class: "swatch", style: `background:${c}`, "aria-label": `Ink ${c}`, "aria-pressed": String(c === ink), onclick: () => { ink = c; swatches.forEach(s => s.setAttribute("aria-pressed", String(s.style.background === s2(c)))); compose(); } }));
      const s2 = c => { const d = document.createElement("div"); d.style.background = c; return d.style.background; };
      const apply = el("button", { class: "primary", type: "button", disabled: true, onclick: async () => { await compose(); out.toBlob(b => { dlg.close(); dlg.remove(); resolve(b); }, "image/png"); } }, "Apply signature");
      const dlg = el("dialog", { class: "wide sigmodal" }, el("h2", {}, title), el("div", { class: "tabs" }, tabs), ...Object.values(panels),
        el("div", { class: "row", style: "margin-top:12px" }, el("span", { class: "muted" }, "Ink"), swatches), el("label", {}, "Preview"), el("div", { class: "checker" }, preview),
        el("div", { class: "row", style: "margin-top:14px" }, apply, el("button", { type: "button", onclick: () => { dlg.close(); dlg.remove(); resolve(null); } }, "Cancel")));
      dlg.addEventListener("cancel", () => { dlg.remove(); resolve(null); });
      const pos = e => { const r = pad.getBoundingClientRect(); return [(e.clientX - r.left) * W / r.width, (e.clientY - r.top) * H / r.height]; };
      pad.addEventListener("pointerdown", e => { drawing = true; pad.setPointerCapture(e.pointerId); pc.beginPath(); pc.moveTo(...pos(e)); });
      pad.addEventListener("pointermove", e => { if (!drawing) return; pc.lineTo(...pos(e)); pc.stroke(); drawn = true; });
      ["pointerup", "pointercancel"].forEach(t => pad.addEventListener(t, () => { drawing = false; compose(); }));
      document.body.append(dlg); dlg.showModal(); compose();
    });
  }
  return { open };
})();
