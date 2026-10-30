const $ = id => document.getElementById(id);
let pages = []; // {blob, url}
let stream = null;

function render() {
  const box = $("preview"); box.replaceChildren();
  pages.forEach((p, i) => box.append(DM.el("figure", {},
    DM.el("img", { src: p.url, alt: `Page ${i + 1}` }),
    DM.el("button", { class: "sm rm", type: "button", "aria-label": `Remove page ${i + 1}`, onclick: () => { URL.revokeObjectURL(p.url); pages.splice(i, 1); render(); } }, "✕"))));
  $("create-pdf").disabled = !pages.length;
}
const add = blob => { pages.push({ blob, url: URL.createObjectURL(blob) }); render(); };

$("scan-files").addEventListener("change", e => { [...e.target.files].forEach(add); e.target.value = ""; });

$("cam-start").addEventListener("click", async () => {
  try {
    stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: { ideal: "environment" }, width: { ideal: 1920 } } });
    $("cam").srcObject = stream; $("cam-panel").hidden = false;
  } catch {
    $("scan-msg").className = "msg error";
    $("scan-msg").textContent = "Camera unavailable. Allow camera access (HTTPS or localhost required) or choose photos instead.";
  }
});
function stopCam() { stream?.getTracks().forEach(t => t.stop()); stream = null; $("cam-panel").hidden = true; }
$("cam-stop").addEventListener("click", stopCam);
$("cam-shot").addEventListener("click", () => {
  const v = $("cam"), c = document.createElement("canvas");
  c.width = v.videoWidth; c.height = v.videoHeight;
  c.getContext("2d").drawImage(v, 0, 0);
  c.toBlob(b => b && add(b), "image/jpeg", 0.9);
});

$("create-pdf").addEventListener("click", async () => {
  const m = $("scan-msg"); m.className = "msg"; m.textContent = "Creating PDF…";
  $("create-pdf").disabled = true;
  const form = new FormData();
  pages.forEach((p, i) => form.append("files", p.blob, `page-${i + 1}.jpg`));
  try {
    await DM.api("/scanner/convert", { method: "POST", body: form });
    stopCam(); location.href = "/dashboard";
  } catch (e) { m.className = "msg error"; m.textContent = e.message; $("create-pdf").disabled = false; }
});
