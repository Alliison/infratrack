/* Painel de TVs da conta (celular). Usa o token de controle guardado pelo
   login (ou pelo QR "Configurar" de uma TV). Atualiza sozinho: o estado de
   cada TV (no ar, tela atual, placas) vem do heartbeat dela. */
const $ = (id) => document.getElementById(id);
const token = TI.ls.get(TI.CTL_KEY);
const REFRESH_MS = 5000;
let timer = null;
let devs = [];
let sgiOn = true;

function semAcesso() {
  clearInterval(timer);
  $("panel").hidden = true;
  $("noaccess").hidden = false;
}

async function api(path, opts = {}) {
  const r = await fetch(path, {
    ...opts,
    headers: { "Content-Type": "application/json", ...TI.auth(token), ...(opts.headers || {}) },
  });
  if (r.status === 401) { TI.ls.del(TI.CTL_KEY); semAcesso(); throw new Error("sem acesso"); }
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.detail || `HTTP ${r.status}`);
  return body;
}

function say(text, cls = "msg") { $("dev-msg").className = cls; $("dev-msg").textContent = text; }

function ago(ts) {
  if (!ts) return "nunca";
  const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
  if (s < 60) return `há ${s}s`;
  if (s < 3600) return `há ${Math.round(s / 60)} min`;
  if (s < 86400) return `há ${Math.round(s / 3600)} h`;
  return `há ${Math.round(s / 86400)} d`;
}

const el = (tag, props = {}, ...kids) => {
  const e = Object.assign(document.createElement(tag), props);
  e.append(...kids.filter((k) => k != null));
  return e;
};
const b = (t) => el("b", { textContent: t });

const TELA = { fulltrack: "FullTrack", sgi: "SGI" };
const LOCKS = [["none", "Alternar"], ["fulltrack", "Só FullTrack"], ["sgi", "Só SGI"]];

function estado(d) {
  if (!d.connected) {
    return [el("span", {}, b("Desconectada"), " — a TV mostra o QR. Liberando de novo, ela volta com esta configuração.")];
  }
  if (!d.online) {
    return [b("Sem sinal"), ` (último contato ${ago(d.last_seen)}) — TV desligada, aba fechada ou sem rede.`];
  }
  const partes = [b("No ar"), " · exibindo ", b(TELA[d.screen] || d.screen || "—")];
  if (d.page) partes.push(` · ${d.page}`);
  if (d.screen === "fulltrack" && d.grid && d.grid !== "–") partes.push(` · grade ${d.grid}`);
  return partes;
}

function resumoConfig(c) {
  const p = [];
  p.push(c.selected_ids?.length ? `${c.selected_ids.length} carro(s) escolhido(s)`
                                 : (c.only_ligados !== false ? "todos os carros ligados" : "todos os carros"));
  p.push(c.rotativo ? `rodízio ${c.rotate_seconds}s` : "sem rodízio");
  if (c.rotativo && c.sgi_in_rotation) p.push("SGI no rodízio");
  if (c.grid && c.grid !== "auto") p.push(`grade ${c.grid}`);
  return p.join(" · ");
}

function card(d) {
  const dot = el("span", { className: "dot" + (d.online ? " on" : d.connected ? " idle" : "") });
  const head = el("div", { className: "dev-head" }, dot, el("span", { className: "dev-name", textContent: d.name }));

  const info = [];
  if (d.connected && d.last_seen) {
    info.push(`visto ${ago(d.last_seen)}`);
    if (d.viewport) info.push(d.viewport);
    if (d.ip) info.push(d.ip);
  }
  const st = el("div", { className: "dev-state" }, ...estado(d),
    info.length ? el("br") : null, info.length ? info.join(" · ") : null,
    el("br"), el("span", { textContent: "Config: " + resumoConfig(d.config) }));

  const aviso = (d.config.screen_lock === "sgi" && !sgiOn)
    ? el("div", { className: "msg err", style: "margin:0 0 10px" },
        "Travada no SGI, mas o SGI está desconectado: a TV mostra o FullTrack até reconectar (Configurar → SGI).")
    : null;

  const plates = d.online && d.screen === "fulltrack" && d.showing?.length
    ? el("div", { className: "plates" }, ...d.showing.map((p) => el("span", { className: "plate", textContent: p })))
    : null;

  // Trava de tela direto daqui: é o ajuste mais comum entre TVs diferentes
  // (uma presa no FullTrack, outra no SGI) e não vale abrir a configuração.
  const lock = el("div", { className: "choice" }, ...LOCKS.map(([v, rot]) => {
    const atual = (d.config.screen_lock || "none") === v;
    // "Só SGI" sem SGI conectado não é escolha válida (o backend recusa);
    // continua visível marcado se já estava, para o aviso fazer sentido.
    const btn = el("button", { type: "button", textContent: rot, className: atual ? "on" : "",
      disabled: v === "sgi" && !sgiOn && !atual,
      title: v === "sgi" && !sgiOn ? "Conecte o SGI primeiro (Configurar → SGI)" : "" });
    btn.addEventListener("click", () => salvarConfig(d, { screen_lock: v }, `${d.name}: ${rot}.`));
    return btn;
  }));

  const acoes = el("div", { className: "dev-actions" });
  const add = (texto, fn, cls = "") => {
    const x = el("button", { type: "button", textContent: texto, className: cls });
    x.addEventListener("click", fn);
    acoes.append(x);
  };
  acoes.append(el("a", { href: `/config?d=${encodeURIComponent(d.id)}`, textContent: "Configurar" }));
  acoes.append(el("a", { href: `/?watch=${encodeURIComponent(d.id)}`, textContent: "Espelhar" }));
  if (d.connected) {
    add("Identificar", () => comando(d, "identify", `${d.name} vai mostrar o nome na tela.`));
    add("Recarregar", () => comando(d, "reload", `${d.name} vai recarregar a página.`));
  }
  add("Renomear", () => renomear(d));
  if (devs.length > 1) add("Copiar config de…", () => copiarDe(d));
  if (d.connected) add("Desconectar", () => desconectar(d), "danger");
  else add("Esquecer", () => esquecer(d), "danger");

  return el("div", { className: "dev" + (d.connected ? "" : " off") }, head, st, aviso, plates, lock, acoes);
}

function renderCtls(ctls) {
  const box = $("ctls");
  box.innerHTML = "";
  if (!ctls.length) { box.append(el("p", { className: "hint", textContent: "Nenhum." })); return; }
  for (const c of ctls) {
    const exp = c.expires_in == null ? "não expira" : `expira se ficar ${Math.max(1, Math.round(c.expires_in / 3600))} h sem uso`;
    const info = el("div", { className: "ctl-info" },
      el("b", { textContent: c.label + (c.current ? " (este celular)" : "") }),
      `usado ${ago(c.last_used_at)} · ${exp}`);
    const x = el("button", { type: "button", textContent: c.current ? "Sair" : "Remover" });
    x.addEventListener("click", async () => {
      if (!confirm(c.current ? "Tirar o acesso deste celular?" : `Remover o acesso de "${c.label}"?`)) return;
      try { await api(`/api/controllers/${encodeURIComponent(c.id)}`, { method: "DELETE" }); load(); }
      catch (e) { say(e.message, "msg err"); }
    });
    box.append(el("div", { className: "ctl-row" }, info, x));
  }
}

async function load() {
  // Não redesenha com um <select>/campo em uso: no celular isso fecharia o menu.
  if (["SELECT", "INPUT"].includes(document.activeElement?.tagName)) return;
  let data;
  try { data = await api("/api/devices"); } catch (_) { return; }
  $("panel").hidden = false;
  $("noaccess").hidden = true;
  $("account").textContent = data.account;
  devs = data.devices;
  sgiOn = data.sgi_enabled !== false;
  const box = $("devices");
  box.innerHTML = "";
  if (!devs.length) box.append(el("p", { className: "hint", textContent: "Nenhuma TV nesta conta ainda." }));
  devs.forEach((d) => box.append(card(d)));
  renderCtls(data.controllers);
}

async function salvarConfig(d, patch, ok) {
  try {
    await api(`/api/config?device=${encodeURIComponent(d.id)}`, {
      method: "POST", body: JSON.stringify({ ...d.config, ...patch }),
    });
    say(ok, "msg ok");
    load();
  } catch (e) { say(e.message, "msg err"); }
}

async function comando(d, cmd, ok) {
  try {
    await api(`/api/devices/${encodeURIComponent(d.id)}/command`, { method: "POST", body: JSON.stringify({ cmd }) });
    say(ok + " (no próximo ciclo dela, alguns segundos)", "msg ok");
  } catch (e) { say(e.message, "msg err"); }
}

async function renomear(d) {
  const name = prompt("Nome da TV", d.name);
  if (!name || name.trim() === d.name) return;
  try {
    await api(`/api/devices/${encodeURIComponent(d.id)}`, { method: "PATCH", body: JSON.stringify({ name }) });
    load();
  } catch (e) { say(e.message, "msg err"); }
}

async function copiarDe(d) {
  const outras = devs.filter((x) => x.id !== d.id);
  const lista = outras.map((x, i) => `${i + 1} — ${x.name}`).join("\n");
  const n = parseInt(prompt(`Copiar a configuração de qual TV para "${d.name}"?\n\n${lista}`, "1"), 10);
  const src = outras[n - 1];
  if (!src) return;
  salvarConfig(d, src.config, `${d.name} agora usa a configuração de ${src.name}.`);
}

async function desconectar(d) {
  if (!confirm(`Desconectar "${d.name}"? Ela volta para o QR; as outras TVs não são afetadas.`)) return;
  try {
    await api(`/api/devices/${encodeURIComponent(d.id)}/disconnect`, { method: "POST" });
    say(`${d.name} desconectada.`, "msg ok");
    load();
  } catch (e) { say(e.message, "msg err"); }
}

async function esquecer(d) {
  if (!confirm(`Apagar "${d.name}" e a configuração dela? Liberar essa TV de novo cria uma TV nova.`)) return;
  try {
    await api(`/api/devices/${encodeURIComponent(d.id)}`, { method: "DELETE" });
    load();
  } catch (e) { say(e.message, "msg err"); }
}

$("kill-all").addEventListener("click", async () => {
  if (!confirm("Desconectar TODAS as TVs e TODOS os celulares desta conta (inclusive este)?")) return;
  try { await api("/api/account/disconnect-all", { method: "POST" }); } catch (_) {}
  TI.ls.del(TI.CTL_KEY);
  semAcesso();
});

$("base-url").textContent = location.origin + "/";
if (!token) semAcesso();
else { load(); timer = setInterval(load, REFRESH_MS); }
