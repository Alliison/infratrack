/* Configuração de UMA TV (cada TV tem a sua).
   - QR "Configurar" da TV: ?c=<uuid>, troca por um token de controle curto já
     apontado para aquela TV (handoff).
   - Painel de TVs do celular: ?d=<id da TV>, com o token de controle guardado.
   - Na própria TV (fallback): token da TV, mexe só nela mesma. */
const $ = (id) => document.getElementById(id);
let token = null;
let deviceId = null;       // null = a TV dona do token (fallback na própria TV)
const dq = (prefix = "?") => deviceId ? `${prefix}device=${encodeURIComponent(deviceId)}` : "";
let fleet = [];
/* Array, não Set: a ORDEM em que os veículos são marcados é a sequência que o
   modal oferece. Um Set descartaria essa informação. */
let selected = [];
let serverCfg = {};        // o que está salvo hoje — base do "Seguir automático"
let escolhaGrid = "auto";

const CELULAS = { "2x2": 4, "2x3": 6 };

async function resolveToken() {
  const qs = new URLSearchParams(location.search);
  const c = qs.get("c");
  if (c) {
    const r = await fetch(`/api/config/handoff/${c}/redeem`, { method: "POST" });
    if (!r.ok) return null;
    const h = await r.json();
    deviceId = h.device_id;
    // URL sem o uuid (uso único): um F5 aqui não deve dar "QR expirado".
    history.replaceState(null, "", `/config?d=${encodeURIComponent(deviceId)}`);
    // Celular que já é controle desta mesma conta fica com o token dele (o do
    // QR é curto); senão, este passa a ser o controle do celular.
    const atual = TI.ls.get(TI.CTL_KEY);
    if (atual) {
      try {
        const me = await (await fetch("/api/me", { headers: TI.auth(atual) })).json();
        if (me.kind === "controller" && me.account === h.account) return atual;
      } catch (_) {}
    }
    TI.ls.set(TI.CTL_KEY, h.access_token);
    return h.access_token;
  }
  deviceId = qs.get("d");
  const ctl = TI.ls.get(TI.CTL_KEY);
  if (ctl && deviceId) return ctl;
  deviceId = null;
  return TI.ls.get(TI.TV_KEY);
}

/* "Configurando: TV da recepção" — sem isso, com duas TVs, ninguém sabe qual
   está mexendo. */
async function mostrarQuem() {
  // Gestão de TVs é do celular-controle; com o token da própria TV não abre.
  if (deviceId) $("to-devices").hidden = false;
  try {
    if (deviceId) {
      const r = await fetch("/api/devices", { headers: TI.auth(token) });
      if (r.ok) {
        const d = (await r.json()).devices.find((x) => x.id === deviceId);
        if (d) $("dev-who").textContent = `Configurando: ${d.name}`;
        return;
      }
    }
    const me = await (await fetch("/api/me", { headers: TI.auth(token) })).json();
    if (me.device_name) $("dev-who").textContent = `Configurando: ${me.device_name}`;
  } catch (_) {}
}

async function init() {
  token = await resolveToken();
  if (!token) {
    $("list").innerHTML = "";
    $("msg").className = "msg err";
    $("msg").textContent = "QR de configuração expirado ou já usado. Abra novamente na TV.";
    $("save").disabled = true;
    return;
  }
  mostrarQuem();
  let cfg = { selected_ids: [], only_ligados: true, zoom: 15, refresh_seconds: 6 };
  try {
    const r = await fetch("/api/config" + dq(), { headers: TI.auth(token) });
    if (r.ok) cfg = await r.json();
  } catch (_) {}
  serverCfg = cfg;
  $("zoom").value = cfg.zoom;
  $("refresh").value = cfg.refresh_seconds;
  $("only_ligados").checked = cfg.only_ligados !== false;
  carregarExibicao(cfg);

  try {
    const r = await fetch("/api/fleet", { headers: { Authorization: `Bearer ${token}` } });
    if (r.status === 401) throw new Error("expired");
    fleet = await r.json();
  } catch (e) {
    $("list").innerHTML = "";
    $("msg").className = "msg err";
    $("msg").textContent = "Acesso expirado. Abra de novo pelo QR \"Configurar\" da TV ou pelo painel de TVs.";
    return;
  }
  selected = [...(cfg.selected_ids || [])];
  // ordena: ligados primeiro, depois por placa/modelo
  fleet.sort((a, b) =>
    (b.ignicao ? 1 : 0) - (a.ignicao ? 1 : 0) ||
    (a.placa || a.modelo || a.id).localeCompare(b.placa || b.modelo || b.id));
  $("filter").addEventListener("input", renderList);
  $("listOnlyLigados").addEventListener("change", renderList);
  renderList();
  initSgi(cfg);
}

/* ---------------- Tela secundária: SGI ----------------
   O backend guarda usuário e senha do SGI só em RAM (mesma política da senha do
   FullTrack), então o estado verdadeiro é sempre o do servidor. `cfg.sgi_enabled`
   diz apenas que já foi habilitado alguma vez: se o servidor responder
   enabled:false com o flag true, houve restart e é preciso logar de novo — e a
   tela diz isso, em vez de mostrar tudo certo e falhar na primeira consulta. */
let sgiCfg = {};

function sgiMsg(texto, classe = "msg") {
  $("sgi-msg").className = classe;
  $("sgi-msg").textContent = texto;
}

/* O que depende do SGI na exibição só pode ser LIGADO com ele conectado (o
   backend também recusa). O que já estava ligado fica como está: desmarcar
   continua possível, e o aviso no painel de TVs explica a situação. */
function liberarOpcoesSgi(enabled) {
  const opt = $("cf-lock").querySelector('option[value="sgi"]');
  opt.disabled = !enabled && $("cf-lock").value !== "sgi";
  opt.textContent = enabled ? "Só SGI" : "Só SGI (conecte o SGI primeiro)";
  const rot = $("cf-sgi-rot");
  rot.disabled = !enabled && !rot.checked;
  $("cf-sgi-off").hidden = enabled;
}

function mostrarSgi({ enabled, username, expires_in, proactive_refresh, last_error }, jaHabilitou) {
  liberarOpcoesSgi(enabled);
  $("sgi-enable").hidden = enabled;
  $("sgi-form").hidden = true;
  $("sgi-on").hidden = !enabled;

  if (enabled) {
    const partes = [`Conectado como ${username}`];
    if (expires_in != null) {
      partes.push(`token expira em ${(expires_in / 3600).toFixed(1)}h (renova sozinho na última hora)`);
    } else {
      partes.push("token sem validade legível: a renovação é reativa, no primeiro erro");
    }
    $("sgi-state").textContent = partes.join(" · ");
    if (last_error) sgiMsg(`Último erro no health check: ${last_error}`, "msg err");
  } else if (jaHabilitou) {
    $("sgi-state").textContent =
      "O SGI foi habilitado antes, mas a credencial se perdeu no restart do backend. Habilite de novo.";
  } else {
    $("sgi-state").textContent =
      "Desligado. Habilite para exibir as ordens de serviço de hoje na segunda tela.";
  }
}

async function carregarSgi(jaHabilitou) {
  try {
    const st = await (await fetch("/api/sgi/status")).json();
    mostrarSgi(st, jaHabilitou || st.was_enabled === true);
    return st;
  } catch (_) {
    $("sgi-state").textContent = "Não foi possível consultar o estado do SGI.";
    return { enabled: false };
  }
}

async function initSgi(cfg) {
  sgiCfg = cfg;
  const st = await carregarSgi(cfg.sgi_enabled === true);

  $("sgi-enable").addEventListener("click", () => {
    $("sgi-enable").hidden = true;
    $("sgi-form").hidden = false;
    sgiMsg("");
    $("sgi-user").focus();
  });

  $("sgi-cancel").addEventListener("click", () => {
    $("sgi-form").hidden = true;
    $("sgi-enable").hidden = false;
    $("sgi-pass").value = ""; $("sgi-totp").value = "";
    sgiMsg("");
  });

  $("sgi-connect").addEventListener("click", async () => {
    const username = $("sgi-user").value.trim();
    const password = $("sgi-pass").value;
    if (!username || !password) return sgiMsg("Informe usuário e senha do SGI.", "msg err");

    const botao = $("sgi-connect");
    botao.disabled = true;
    sgiMsg("Conectando ao SGI…");
    try {
      const r = await fetch("/api/sgi/enable", {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
        body: JSON.stringify({ username, password, totp_secret: $("sgi-totp").value.trim() || null }),
      });
      const body = await r.json();
      if (!r.ok) throw new Error(body.detail || "falha ao conectar");
      // A senha sai da tela assim que o backend a aceita: ela já vive na RAM dele.
      $("sgi-pass").value = "";
      $("sgi-totp").value = "";
      mostrarSgi(body, true);
      sgiMsg("SGI conectado.", "msg ok");
      carregarEquipes();
    } catch (e) {
      sgiMsg(e.message, "msg err");
    } finally {
      botao.disabled = false;
    }
  });

  $("sgi-disable").addEventListener("click", async () => {
    try {
      const st2 = await (await fetch("/api/sgi/disable", {
        method: "POST", headers: { Authorization: `Bearer ${token}` },
      })).json();
      mostrarSgi(st2, false);
      sgiMsg("SGI desabilitado.", "msg");
    } catch (_) {
      sgiMsg("Não foi possível desabilitar.", "msg err");
    }
  });

  $("sgi-test").addEventListener("click", async () => {
    sgiMsg("Consultando o resumo de hoje…");
    try {
      const r = await fetch("/api/sgi/summary" + dq(), { headers: { Authorization: `Bearer ${token}` } });
      const body = await r.json();
      if (!r.ok) throw new Error(body.detail || `HTTP ${r.status}`);
      $("sgi-out").hidden = false;
      $("sgi-out").textContent = JSON.stringify(body, null, 2);
      sgiMsg("Resumo recebido. É esta a estrutura que a tela secundária vai exibir.", "msg ok");
    } catch (e) {
      sgiMsg(e.message, "msg err");
    }
  });

  $("sgi-team").addEventListener("change", salvarEquipe);
  if (st.enabled) carregarEquipes();

  // Tipo de OS: ao contrário da equipe, não vem do SGI (não existe endpoint
  // de enum pra isso) — são os únicos valores que o `type` do relatório aceita
  // na prática (mesmo domínio do `UserRoleTypeEnum` da spec, tirando "user").
  // `null` no servidor (nunca mexeu nisso pelo celular) reflete como "infra"
  // marcado, porque é o padrão que o backend já aplica nesse caso.
  refletirTipos(cfg.sgi_types ?? ["infra"]);
  document.querySelectorAll("#sgi-types button").forEach((b) => {
    b.addEventListener("click", () => {
      b.classList.toggle("on");
      salvarTipos();
    });
  });
}

function refletirTipos(tipos) {
  document.querySelectorAll("#sgi-types button").forEach((b) => {
    b.classList.toggle("on", tipos.includes(b.dataset.type));
  });
}

function tiposSelecionados() {
  return [...document.querySelectorAll("#sgi-types button.on")].map((b) => b.dataset.type);
}

/* Salva assim que muda, igual à equipe: lista vazia é "todos os tipos"
   escolhido explicitamente, não "esqueceu de marcar". */
async function salvarTipos() {
  const tipos = tiposSelecionados();
  const cfg = { ...base(), sgi_types: tipos };
  try {
    const r = await fetch("/api/config" + dq(), {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
      body: JSON.stringify(cfg),
    });
    if (!r.ok) throw new Error();
    serverCfg = cfg;
    sgiMsg(tipos.length ? `Tipo(s) salvo(s): ${tipos.join(", ")}.` : "Filtro de tipo removido (todos).", "msg ok");
  } catch (_) {
    sgiMsg("Erro ao salvar o tipo.", "msg err");
  }
}

/* Equipes vêm do SGI (/api/core/v1/teams/me, com fallback para /teams/) — nunca
   de lista mantida aqui. Antes este trecho varria endpoints chutados e nunca
   populava o <select>, o que fazia parecer que o SGI não tinha equipes. */
async function carregarEquipes() {
  const hint = $("sgi-team-hint");
  const sel = $("sgi-team");
  hint.textContent = "Carregando equipes…";
  try {
    const r = await fetch("/api/sgi/teams", { headers: { Authorization: `Bearer ${token}` } });
    const equipes = await r.json();
    if (!r.ok) throw new Error(equipes.detail || `HTTP ${r.status}`);

    // Reconstrói as opções: "Todas" + o que o SGI devolveu, nesta ordem.
    sel.innerHTML = '<option value="">Todas as equipes</option>';
    for (const eq of equipes) {
      const o = document.createElement("option");
      o.value = eq.id;
      o.textContent = eq.name || eq.id;
      sel.appendChild(o);
    }
    // Reflete o que está salvo; se a equipe salva não existe mais, volta a "Todas".
    sel.value = equipes.some((e) => e.id === serverCfg.sgi_team) ? serverCfg.sgi_team : "";
    hint.textContent = equipes.length
      ? `${equipes.length} equipe(s) do SGI. A data é sempre hoje: o painel é real time.`
      : "O SGI não devolveu equipes para este usuário.";
    await conferirContrato();
  } catch (e) {
    hint.textContent = `Não foi possível carregar as equipes: ${e.message}`;
  }
}

/* O filtro de equipe depende do parâmetro `teamId` do SGI. Se ele sair da spec,
   a consulta passaria a devolver o total geral sem reclamar — que é pior do que
   falhar. Então a tela confere o contrato e avisa. */
async function conferirContrato() {
  try {
    const d = await (await fetch("/api/sgi/discover", {
      headers: { Authorization: `Bearer ${token}` },
    })).json();
    if (d.spec_disponivel && d.summary_aceita_teamId === false) {
      sgiMsg("Atenção: o SGI não expõe mais o filtro `teamId`. A consulta traria o total geral.", "msg err");
    }
  } catch (_) { /* diagnóstico opcional: silêncio não atrapalha o uso */ }
}

/* Salva a equipe assim que muda: é uma escolha só, não vale exigir "Aplicar". */
async function salvarEquipe() {
  const valor = $("sgi-team").value || null;
  const cfg = { ...base(), sgi_team: valor };
  try {
    const r = await fetch("/api/config" + dq(), {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
      body: JSON.stringify(cfg),
    });
    if (!r.ok) throw new Error();
    serverCfg = cfg;
    sgiMsg(valor ? "Equipe salva." : "Filtro de equipe removido (todas).", "msg ok");
  } catch (_) {
    sgiMsg("Erro ao salvar a equipe.", "msg err");
  }
}

// Formata placa BR: ABC1234 -> ABC-1234; Mercosul (ABC1D23) fica como está.
function formatPlaca(p) {
  if (!p) return "";
  const s = p.toUpperCase().replace(/[^A-Z0-9]/g, "");
  return /^[A-Z]{3}[0-9]{4}$/.test(s) ? s.slice(0, 3) + "-" + s.slice(3) : s;
}

function nomeDe(v) {
  return formatPlaca(v.placa) || v.modelo || v.id;
}

function renderList() {
  const q = ($("filter").value || "").trim().toLowerCase();
  const onlyLig = $("listOnlyLigados").checked;
  const list = $("list");
  list.innerHTML = "";
  const rows = fleet.filter(v =>
    (!onlyLig || v.ignicao) &&
    (!q || (v.placa || "").toLowerCase().includes(q) || (v.modelo || "").toLowerCase().includes(q)));
  if (!rows.length) { list.innerHTML = '<div class="veh-row veh-modelo">Nenhum veículo encontrado.</div>'; }
  for (const v of rows) {
    const row = document.createElement("label");
    row.className = "veh-row";
    const pos = selected.indexOf(v.id);
    row.innerHTML = `
      <input type="checkbox" value="${v.id}" ${pos >= 0 ? "checked" : ""}>
      <div class="veh-info">
        <div class="veh-placa">${nomeDe(v)}${pos >= 0 ? ` <span class="seq-num">${pos + 1}º</span>` : ""}</div>
        <div class="veh-modelo">${v.modelo || "—"}</div>
      </div>
      <span class="badge ${v.ignicao ? "on" : "off"}">${v.ignicao ? "Ligado" : "Desligado"}</span>`;
    row.querySelector("input").addEventListener("change", (e) => {
      if (e.target.checked) selected.push(v.id);
      else selected = selected.filter(id => id !== v.id);
      renderList();   // renumera as posições exibidas
    });
    list.appendChild(row);
  }
  updateCount();
}

function updateCount() {
  $("selcount").textContent = selected.length ? `(${selected.length} selecionados)` : "(todos)";
}

$("clear").addEventListener("click", () => {
  selected = [];
  renderList();
  $("msg").className = "msg";
  $("msg").textContent = "Seleção limpa. Selecione os veículos e continue.";
});

/* ---------------- Modal de confirmação ---------------- */
/* Exibição da TV (rodízio, tempo, SGI no rodízio, trava): mora na página, não
   no modal, e é salva por conta própria. Vai junto em qualquer outro salvamento
   via base(), então nunca é sobrescrita pelo layout. */
function carregarExibicao(cfg) {
  $("cf-rotativo").checked = !!cfg.rotativo;
  $("cf-sgi-rot").checked = !!cfg.sgi_in_rotation;
  $("cf-lock").value = cfg.screen_lock || "none";
  $("cf-techs-open").checked = !!cfg.techs_expanded;
  $("cf-rotate").value = cfg.rotate_seconds || 15;
}

function abrirConfirm() {
  escolhaGrid = serverCfg.grid || "auto";
  $("cf-page-size").value = serverCfg.page_size || 9;
  pintarGrid();
  renderSeq();
  $("confirm").classList.add("show");
}

function fecharConfirm() { $("confirm").classList.remove("show"); }

function pintarGrid() {
  for (const b of $("cf-grid").querySelectorAll("button")) {
    b.classList.toggle("on", b.dataset.grid === escolhaGrid);
  }
  // Com grade fixa o número de telas por página é consequência dela (2×2 = 4),
  // então o campo vira informativo em vez de aceitar um valor conflitante.
  const fixo = CELULAS[escolhaGrid];
  $("cf-page-size").disabled = !!fixo;
  if (fixo) $("cf-page-size").value = fixo;
  avaliar();
}

function renderSeq() {
  const box = $("cf-seq");
  box.innerHTML = "";
  if (!selected.length) {
    box.innerHTML = '<div class="sub">Nenhum veículo marcado — a TV escolhe sozinha quais exibir.</div>';
    return avaliar();
  }
  const byId = new Map(fleet.map(v => [v.id, v]));
  selected.forEach((id, i) => {
    const v = byId.get(id) || { id };
    const row = document.createElement("div");
    row.className = "seq-row";
    row.innerHTML = `
      <span class="seq-num">${i + 1}º</span>
      <span class="seq-nome">${nomeDe(v)}</span>
      <button type="button" class="seq-btn" data-mov="-1" ${i === 0 ? "disabled" : ""}>↑</button>
      <button type="button" class="seq-btn" data-mov="1" ${i === selected.length - 1 ? "disabled" : ""}>↓</button>`;
    for (const b of row.querySelectorAll("button")) {
      b.addEventListener("click", () => {
        const j = i + Number(b.dataset.mov);
        [selected[i], selected[j]] = [selected[j], selected[i]];
        renderSeq();
      });
    }
    box.appendChild(row);
  });
  avaliar();
}

/* Avisa quando a escolha esconde veículos: grade fixa menor que a seleção e sem
   rodízio significa que os últimos da fila simplesmente não aparecem. Melhor
   dizer isso aqui do que deixar a pessoa descobrir olhando a parede. */
function avaliar() {
  const fixo = CELULAS[escolhaGrid];
  const aviso = $("cf-aviso");
  if (fixo && selected.length > fixo && !$("cf-rotativo").checked) {
    aviso.className = "msg err";
    aviso.textContent = `${selected.length} veículos não cabem em ${escolhaGrid} sem rodízio: ` +
                        `só os ${fixo} primeiros vão aparecer. Ligue o rodízio para ver todos.`;
  } else {
    aviso.className = "msg";
    aviso.textContent = "";
  }
}

async function salvar(cfg, botao, msgEl = $("msg"), okTexto = "Mosaico salvo! ✅") {
  msgEl.className = "msg";
  msgEl.textContent = "Salvando…";
  botao.disabled = true;
  try {
    const r = await fetch("/api/config" + dq(), {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
      body: JSON.stringify(cfg),
    });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || "Erro ao salvar.");
    serverCfg = cfg;
    fecharConfirm();
    msgEl.className = "msg ok";
    msgEl.textContent = okTexto;
  } catch (e) {
    msgEl.className = "msg err";
    msgEl.textContent = e.message || "Erro ao salvar.";
  } finally {
    botao.disabled = false;
  }
}

// O que sempre vai junto, escolhendo layout ou não.
/* Parte do que o servidor já tem e sobrescreve só o que esta tela controla.
   O POST /api/config substitui o MosaicConfig INTEIRO: enumerar os campos aqui
   fazia qualquer campo novo (os do SGI, por exemplo) voltar ao default a cada
   "Aplicar". Espalhar o serverCfg primeiro resolve isso para sempre — inclusive
   para campos que ainda não existem. */
function base() {
  return {
    ...serverCfg,
    selected_ids: [...selected],
    only_ligados: $("only_ligados").checked,
    zoom: parseInt($("zoom").value, 10) || 15,
    refresh_seconds: parseInt($("refresh").value, 10) || 6,
    rotativo: $("cf-rotativo").checked,
    sgi_in_rotation: $("cf-sgi-rot").checked,
    screen_lock: $("cf-lock").value,
    techs_expanded: $("cf-techs-open").checked,
    rotate_seconds: parseInt($("cf-rotate").value, 10) || 15,
  };
}

$("tv-save").addEventListener("click", (e) => {
  e.preventDefault();
  salvar(base(), $("tv-save"), $("tv-msg"), "Exibição salva! ✅");
});

$("save").addEventListener("click", (e) => { e.preventDefault(); abrirConfirm(); });
$("cf-cancel").addEventListener("click", fecharConfirm);
$("cf-rotativo").addEventListener("change", avaliar);
for (const b of $("cf-grid").querySelectorAll("button")) {
  b.addEventListener("click", () => { escolhaGrid = b.dataset.grid; pintarGrid(); });
}

$("cf-apply").addEventListener("click", (e) => {
  e.preventDefault();
  const fixo = CELULAS[escolhaGrid];
  salvar({
    ...base(),
    grid: escolhaGrid,
    sequencia_manual: selected.length > 0,
    page_size: fixo || parseInt($("cf-page-size").value, 10) || 9,
  }, $("cf-apply"));
});

/* Seguir automático: guarda os veículos marcados e devolve o layout ao
   comportamento de sempre (grade pelo número de carros, ligados na frente).
   Rodízio e tempos (bloco "Exibição das telas") não são tocados por este
   botão — ele existe para NÃO impor nada novo ao layout. */
$("cf-auto").addEventListener("click", (e) => {
  e.preventDefault();
  salvar({
    ...base(),
    grid: "auto",
    sequencia_manual: false,
    page_size: serverCfg.page_size || 9,
  }, $("cf-auto"));
});

init();
