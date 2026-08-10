/* Página aberta no celular ao escanear o QR. Envia as credenciais do FullTrack
   ao backend, que faz o login e libera a TV vinculada ao UUID da sessão. */
const params = new URLSearchParams(location.search);
const sessionUuid = params.get("s");
const form = document.getElementById("form");
const msg = document.getElementById("msg");
const btn = document.getElementById("submit");

if (!sessionUuid) {
  msg.className = "msg err";
  msg.textContent = "Link inválido. Escaneie o QR-code novamente na TV.";
  btn.disabled = true;
}

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  btn.disabled = true;
  msg.className = "msg";
  msg.textContent = "Entrando…";
  try {
    const r = await fetch("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        session_uuid: sessionUuid,
        login: document.getElementById("login").value.trim(),
        password: document.getElementById("password").value,
      }),
    });
    const data = await r.json().catch(() => ({}));
    if (r.ok) {
      msg.className = "msg ok";
      msg.textContent = "Painel liberado! Pode olhar para a TV. ✅";
      form.querySelectorAll("input").forEach(i => i.disabled = true);
    } else {
      msg.className = "msg err";
      msg.textContent = data.detail || "Não foi possível entrar.";
      btn.disabled = false;
    }
  } catch (_) {
    msg.className = "msg err";
    msg.textContent = "Erro de conexão. Tente de novo.";
    btn.disabled = false;
  }
});
