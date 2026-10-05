/* Página aberta no celular ao escanear o QR da TV.

   Dois caminhos:
   - celular já é CONTROLE de uma conta (token de controle guardado): oferece
     "Liberar sem senha" — a TV entra na conta usando a sessão do FullTrack que
     ela já tem;
   - senão (ou "outra conta"): usuário e senha do FullTrack. O backend libera a
     TV e devolve um token de controle, e este celular passa a gerenciar a conta. */
const params = new URLSearchParams(location.search);
const sessionUuid = params.get("s");
const form = document.getElementById("form");
const msg = document.getElementById("msg");
const btn = document.getElementById("submit");
const $ = (id) => document.getElementById(id);

function say(text, cls = "msg") { msg.className = cls; msg.textContent = text; }

function done(device) {
  $("pair").hidden = true;
  form.hidden = true;
  say(`${device?.name || "TV"} liberada! Pode olhar para a TV. ✅`, "msg ok");
  $("after").hidden = false;
}

async function init() {
  if (!sessionUuid) {
    say("Link inválido. Escaneie o QR-code novamente na TV.", "msg err");
    btn.disabled = true;
    return;
  }
  const ctl = TI.ls.get(TI.CTL_KEY);
  if (!ctl) return;
  try {
    const r = await fetch("/api/me", { headers: TI.auth(ctl) });
    if (!r.ok) { TI.ls.del(TI.CTL_KEY); return; }
    const me = await r.json();
    if (me.kind !== "controller") return;
    $("pair-who").innerHTML = "";
    $("pair-who").append("Este celular já gerencia a conta ",
      Object.assign(document.createElement("b"), { textContent: me.account }),
      ". Libere esta TV nela sem digitar a senha.");
    $("pair").hidden = false;
    form.hidden = true;
  } catch (_) { /* sem rede: fica o formulário */ }
}

$("pair-other").addEventListener("click", () => {
  $("pair").hidden = true;
  form.hidden = false;
  $("login").focus();
});

$("pair-btn").addEventListener("click", async () => {
  const b = $("pair-btn");
  b.disabled = true;
  say("Liberando…");
  try {
    const r = await fetch("/api/auth/pair", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...TI.auth(TI.ls.get(TI.CTL_KEY)) },
      body: JSON.stringify({ session_uuid: sessionUuid, name: $("pair-name").value.trim() || null }),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) {
      if (r.status === 401) {               // controle expirou/revogado: volta à senha
        TI.ls.del(TI.CTL_KEY);
        $("pair").hidden = true; form.hidden = false;
      }
      throw new Error(data.detail || "Não foi possível liberar.");
    }
    done(data.device);
  } catch (e) {
    say(e.message, "msg err");
    b.disabled = false;
  }
});

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  btn.disabled = true;
  say("Entrando…");
  try {
    const r = await fetch("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        session_uuid: sessionUuid,
        login: $("login").value.trim(),
        password: $("password").value,
      }),
    });
    const data = await r.json().catch(() => ({}));
    if (r.ok) {
      if (data.controller_token) TI.ls.set(TI.CTL_KEY, data.controller_token);
      form.querySelectorAll("input").forEach(i => i.disabled = true);
      done(data.device);
    } else {
      say(data.detail || "Não foi possível entrar.", "msg err");
      btn.disabled = false;
    }
  } catch (_) {
    say("Erro de conexão. Tente de novo.", "msg err");
    btn.disabled = false;
  }
});

init();
