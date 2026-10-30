let registering = false;
const $ = id => document.getElementById(id);
const msg = $("msg");

if (DM.token()) location.href = "/dashboard";

const toggle = $("toggle");
if (toggle) toggle.addEventListener("click", e => {
  e.preventDefault();
  registering = !registering;
  $("name-row").hidden = !registering;
  $("pw-hint").hidden = !registering;
  $("submit").textContent = registering ? "Create account" : "Sign in";
  toggle.textContent = registering ? "I already have an account" : "Create an account";
  $("password").autocomplete = registering ? "new-password" : "current-password";
  msg.textContent = "";
});

$("auth-form").addEventListener("submit", async e => {
  e.preventDefault();
  const btn = $("submit");
  btn.disabled = true; msg.className = "msg"; msg.textContent = "";
  try {
    let data;
    if (registering) {
      data = await DM.api("/auth/register", {
        method: "POST", noRedirect: true,
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: $("email").value, name: $("name").value, password: $("password").value }),
      });
    } else {
      data = await DM.api("/auth/token", {
        method: "POST", noRedirect: true,
        body: new URLSearchParams({ username: $("email").value, password: $("password").value }),
      });
    }
    DM.setToken(data.access_token);
    location.href = "/dashboard";
  } catch (err) {
    msg.className = "msg error"; msg.textContent = err.message;
  } finally { btn.disabled = false; }
});
