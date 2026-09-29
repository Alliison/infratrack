/* TrackInfra — tela secundária (SGI).
   Página própria: nada de Leaflet, nada do mosaico da FullTrack. Reaproveita só
   o token de sessão da TV (mesmo QR de login) para chamar /api/sgi/summary. */

const TOKEN_KEY = "trackinfra_token";
let token = localStorage.getItem(TOKEN_KEY);
let refreshSeconds = 6;
let secTimer = null;
let authFails = 0;                 // 401 seguidos; só o limite abaixo volta ao QR
const AUTH_FAILS_MAX = 2;
// Dentro do mosaico esta página vive num iframe que fica montado o tempo todo.
// Ali a rotação (cidade/técnico abertos) avança UM passo por visita: a cada vez
// que o rodízio traz o SGI para a tela — ou seja, no ritmo do mosaico, uma volta
// do FullTrack, uma volta do SGI. Enquanto está na tela os dados atualizam sem
// girar, e escondida nada avança. Aberta direto (fora de iframe) mantém o
// ritmo antigo: um passo por atualização.
const standalone = window.parent === window;
let visible = standalone;
let locked = false;                 // travada na tela: gira a cada atualização, como fora do iframe
let advanceOnce = false;            // 1 passo pendente, consumido no próximo desenho
window.addEventListener("message", (e) => {
  if (e.origin !== location.origin || !e.data || typeof e.data.sgiVisible !== "boolean") return;
  const was = visible;
  visible = e.data.sgiVisible;
  locked = !!e.data.sgiLocked;
  if (visible && !was && token) { advanceOnce = true; refreshSecondary(); }
});

let openCity = null;                // nome da cidade aberta no acordeão (1 por vez)
let cityRotationIndex = -1;          // avança 1 por ciclo; rotação automática, sem clique

const $ = (id) => document.getElementById(id);

/* Erro passageiro: mantém a tela e só avisa no rodapé. */
function stale(msg) { $("updated").textContent = msg; }

/* ---------------- Autenticação por QR ---------------- */
async function startAuth() {
  $("auth").classList.add("show");
  clearInterval(secTimer); secTimer = null;
  const r = await fetch("/api/auth/session", { method: "POST" });
  const s = await r.json();
  $("qrimg").src = s.qr_url;
  $("authurl").value = s.login_url;
  $("authhint").textContent = "Aguardando leitura…";
  pollAuth(s.session_uuid, Date.now() + s.expires_in * 1000);
}

async function pollAuth(uuid, deadline) {
  if (Date.now() > deadline) {
    $("authhint").innerHTML = '<span class="expired">QR-code expirado.</span> Gerando novo…';
    return setTimeout(startAuth, 1500);
  }
  try {
    const r = await fetch(`/api/auth/status/${uuid}`);
    const s = await r.json();
    if (s.status === "authorized" && s.access_token) {
      token = s.access_token;
      localStorage.setItem(TOKEN_KEY, token);
      $("auth").classList.remove("show");
      return boot();
    }
    if (s.status === "expired") return startAuth();
  } catch (_) { /* rede — tenta de novo */ }
  setTimeout(() => pollAuth(uuid, deadline), 2000);
}

/* O mosaico embute esta tela num iframe (rodízio) e os dois dividem o token: quando um faz o
   login (ou o perde), o outro acompanha em vez de mostrar um QR próprio. */
window.addEventListener("storage", (e) => {
  if (e.key !== TOKEN_KEY || e.newValue === token) return;
  token = e.newValue;
  if (token) { $("auth").classList.remove("show"); boot(); }
});

/* ---------------- Ciclo de dados ---------------- */
async function boot() {
  try {
    const cfg = await (await fetch("/api/config")).json();
    refreshSeconds = Math.max(3, cfg.refresh_seconds || 6);
  } catch (_) {}
  refreshSecondary();
  clearInterval(secTimer);
  secTimer = setInterval(refreshSecondary, refreshSeconds * 1000);
}

// Lê só o que pode mudar com a TV já no ar (a configuração salva no celular
// vale no próximo ciclo, sem recarregar a página). Falha = mantém o que tinha.
let techsExpanded = false;
async function refreshDisplayConfig() {
  try {
    const cfg = await (await fetch("/api/config")).json();
    techsExpanded = !!cfg.techs_expanded;
  } catch (_) {}
}

async function refreshSecondary() {
  if (!token) return startAuth();
  await refreshDisplayConfig();
  let body;
  try {
    const r = await fetch("/api/sgi/summary", { headers: { Authorization: `Bearer ${token}` } });
    if (r.status === 401) {
      // O backend religa sozinho no FullTrack, então 401 aqui é sessão perdida
      // de vez (restart do backend ou senha trocada). Tolera blips antes de
      // mandar a TV para o QR — ela não deve piscar por um erro passageiro.
      if (++authFails < AUTH_FAILS_MAX) return stale("reconectando…");
      localStorage.removeItem(TOKEN_KEY); token = null; authFails = 0;
      return startAuth();
    }
    body = await r.json();
    if (r.status === 409) return renderCompletionOff(body.detail);
    if (!r.ok) return stale("SGI indisponível — tentando de novo…");
  } catch (_) { return stale("sem rede — tentando de novo…"); }
  authFails = 0;

  renderCompletion(body);
  $("updated").textContent = "atualizado " + new Date().toLocaleTimeString("pt-BR");
}

// SGI ainda não habilitado (ou credencial perdida no restart do backend): a
// tela avisa em vez de mostrar zeros, que pareceriam dado real.
function renderCompletionOff(detail) {
  $("sec-msg").className = "msg err";
  $("sec-msg").textContent = detail || "SGI não habilitado. Configure pelo celular.";
  $("sec-todo").textContent = "–";
  $("sec-inprogress").textContent = "–";
  $("sec-expired").textContent = "–";
  renderCities([]);
  renderTechnicians([]);
}

// `total`, `completed`, `unproductive`, `closed`, `scheduled`, `rescheduled`,
// `inProgress` e `expired` vêm prontos do relatório do SGI
// (ServiceOrderReportSummaryResponse) — nada calculado aqui a mais que as
// somas que os cards pedem: closed+completed+unproductive é "tudo que a
// equipe já mexeu hoje" (produtivo, improdutivo ou só fechado), e
// scheduled+rescheduled é o que ainda falta rodar hoje. `inProgress` e
// `expired` são passthrough direto, sem soma.
function renderCompletion(s) {
  const total = s.total || 0;
  const completed = s.completed || 0;
  const unproductive = s.unproductive || 0;
  const closed = s.closed || 0;
  $("sec-done").textContent = closed + completed + unproductive;
  $("sec-total2").textContent = total;
  $("sec-todo").textContent = (s.scheduled || 0) + (s.rescheduled || 0);
  $("sec-inprogress").textContent = s.inProgress || 0;
  $("sec-expired").textContent = s.expired || 0;
  const advance = standalone || locked || advanceOnce;
  advanceOnce = false;
  renderCities(s.ordersByCity || [], advance);
  renderTechnicians(s.ordersByTechnician || [], advance);
  refreshTechStatus();
  $("sec-msg").className = "msg";
  $("sec-msg").textContent = "";
}

// Área da direita — 1º rascunho. `ordersByTechnician` já vem pronto do SGI:
// name/count/completed/unproductive/avgDurationInMinutes por técnico, nada
// calculado aqui além de formatar os minutos.
let techRows = [];        // último ordersByTechnician, pra redesenhar quando o status chegar
let techInfo = {};
let techLoaded = false;   // false até a 1ª resposta do /technicians/ (sem isso "sem OS" pisca no boot)        // {id do técnico: {status, isAvailable, ...}} do /technicians/
// Valores de `status` que contam como técnico ativo (a spec não lista o enum:
// ajustar aqui depois de ver os valores reais).
const ACTIVE_STATUS = new Set(["active", "ativo", "online", "available"]);

async function refreshTechStatus() {
  try {
    const r = await fetch("/api/sgi/technicians", { headers: { Authorization: `Bearer ${token}` } });
    if (!r.ok) return;
    techInfo = await r.json();
    techLoaded = true;
    renderTechnicians(techRows);
  } catch (_) { /* mantém a legenda anterior */ }
}

let openTech = null;              // id do técnico aberto (1 por vez, rotação automática)
let techRotationIndex = -1;

// Uma parede, ninguém clica: como nas cidades, o técnico aberto avança 1 por
// ciclo de dados (`advance`). O redesenho que só traz o status novo
// (refreshTechStatus) mantém o mesmo aberto.
function renderTechnicians(rows, advance = false) {
  techRows = rows;
  const container = $("sec-technicians");
  container.innerHTML = "";
  // Todas as gavetas abertas: a tabela reparte a altura entre os técnicos e
  // `--ts` encolhe fontes/espaços conforme a quantidade (até 8 cabe em
  // tamanho cheio; acima disso escala proporcionalmente, com piso).
  const allOpen = techsExpanded && rows.length > 0;
  container.parentElement.classList.toggle("all-open", allOpen);
  container.parentElement.style.setProperty("--ts", String(Math.max(0.5, Math.min(1, 8 / Math.max(rows.length, 1)))));
  if (!rows.length) { openTech = null; techRotationIndex = -1; }
  else if (advance || !rows.some((t) => t.id === openTech)) {
    techRotationIndex = (techRotationIndex + 1) % rows.length;
    openTech = rows[techRotationIndex].id;
  }
  for (const t of rows) {
    const item = document.createElement("div");
    item.className = "tech-item" + (allOpen || t.id === openTech ? " open" : "");
    const row = document.createElement("div");
    row.className = "tech-row";

    const name = document.createElement("span");
    name.className = "tech-name";
    name.textContent = t.name || "—";

    const info = techInfo[t.id] || {};
    const active = ACTIVE_STATUS.has(String(info.status || "").toLowerCase());

    // Avatar antes do nome: verde = ativo, cinza = inativo/desconhecido.
    const avatar = document.createElement("i");
    avatar.className = "fa-solid fa-circle-user tech-avatar " + (!techLoaded ? "sk-avatar" : active ? "on" : "off");
    avatar.title = info.status || "sem status";

    // Legenda sob o nome; 1º badge = isAvailable.
    const sub = document.createElement("span");
    sub.className = "tech-sub";
    if (typeof info.isAvailable === "boolean") {
      const badge = document.createElement("span");
      badge.className = "badge " + (info.isAvailable ? "ok" : "off");
      badge.textContent = info.isAvailable ? "Disponível" : "Indisponível";
      const os = info.currentServiceOrder;
      badge.title = os ? `Em OS ${os.code || ""} — ${os.category || os.type || ""} (${os.status || ""})`
                       : "Sem OS em execução";
      sub.append(badge);
    }
    // 2º badge = status da OS em execução (mesmo texto da coluna "Status" do
    // detalhe). Sem OS, não aparece — o detalhe já avisa "Sem OS em execução".
    const cur = info.currentServiceOrder;
    if (cur && cur.status) {
      const osBadge = document.createElement("span");
      osBadge.className = "badge os-" + (OS_STATUS_TONE[cur.status] || "idle");
      osBadge.textContent = OS_STATUS_LABEL[cur.status] || cur.status;
      osBadge.title = `OS ${cur.code || ""}`.trim();
      sub.append(osBadge);
    }

    const who = document.createElement("div");
    who.className = "tech-who";
    who.append(name, sub);

    const ident = document.createElement("div");
    ident.className = "tech-ident";
    ident.append(avatar, who);

    const yieldEl = document.createElement("span");
    yieldEl.className = "tech-col";
    // total/(concluídas + improdutivas), ex.: 8/5
    yieldEl.textContent = `${t.count || 0}/${(t.completed || 0) + (t.unproductive || 0)}`;

    row.append(ident, yieldEl);
    item.append(row, renderTechDetail(info.currentServiceOrder, techLoaded));
    container.appendChild(item);
  }
}

// Status da OS como o SGI manda -> texto da tela. Valor desconhecido aparece
// cru, pra nunca esconder um status novo.
const OS_STATUS_LABEL = {
  scheduled: "Agendada", rescheduled: "Agendada", in_motion: "Em deslocamento",
  arrived: "No local", checklist: "Checklist", provision: "Provisionamento",
  products_used: "Produtos utilizados",
};

// Cor do badge da OS por fase: a caminho/no local (âmbar), em atendimento
// (azul). Status novo/desconhecido cai em cinza, como o texto cru.
const OS_STATUS_TONE = {
  in_motion: "warn", arrived: "warn",
  checklist: "work", provision: "work", products_used: "work",
};

// Horário em Brasília, independente do fuso do navegador da TV. Sem sufixo de
// fuso ("2026-09-29T15:00:00") o JS leria como horário local; o SGI manda
// UTC, então ali assume-se UTC.
function formatClock(iso) {
  if (!iso) return "—";
  const d = new Date(/(Z|[+-]\d\d:?\d\d)$/.test(iso) ? iso : iso + "Z");
  if (isNaN(d)) return "—";
  return d.toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit",
                                         timeZone: "America/Sao_Paulo" });
}

// Unidade do `category.sla` do SGI: horas (a spec não diz; confirmado com dados
// reais: 0.67 = 40min, 1.5 = 1h30).
const SLA_MINUTES = 60;

// "Duração": tempo desde o início contra o SLA planejado da categoria,
// colorido pelo consumo (verde < 75%, âmbar até 100%, vermelho acima). O
// horário de início fica pequeno embaixo. Sem início (agendada, a caminho,
// no local): "Não iniciada".
function fillDuration(v, cell, os) {
  if (!os.startedAt) { v.textContent = "Não iniciada"; v.classList.add("not-started"); return; }
  const t = new Date(os.startedAt).getTime();
  if (isNaN(t)) { v.textContent = "—"; return; }
  const elapsed = Math.max(0, Math.round((Date.now() - t) / 60000));
  const sla = Number(os.sla) > 0 ? Number(os.sla) * SLA_MINUTES : null;
  const done = elapsed ? formatDuration(elapsed) : "0min";
  v.textContent = sla ? `${done} / ${formatDuration(sla)}` : done;
  if (sla) {
    const r = elapsed / sla;
    v.classList.add(r > 1 ? "sla-over" : r >= 0.75 ? "sla-warn" : "sla-ok");
  }
  const at = document.createElement("span");
  at.className = "since";
  at.textContent = "desde " + formatClock(os.startedAt);
  cell.append(at);
}

// Detalhe do técnico aberto: OS que ele executa (número | endereço | status |
// início) ou um aviso bem visível de que não tem OS.
function renderTechDetail(os, loaded) {
  const box = document.createElement("div");
  box.className = "tech-detail";
  if (!loaded) {   // ainda não sabemos: não afirmar "sem OS"
    box.classList.add("skeleton");
    for (let i = 0; i < 4; i++) {
      const cell = document.createElement("div");
      cell.className = "tech-detail-cell";
      cell.innerHTML = '<span class="sk sk-lbl"></span><span class="sk sk-val"></span>';
      box.append(cell);
    }
    return box;
  }
  if (!os) {
    box.classList.add("empty");
    box.innerHTML = '<i class="fa-solid fa-triangle-exclamation"></i> Sem OS em execução';
    return box;
  }
  const a = os.customerAddress || {};
  const address = [[a.street, a.streetNumber].filter(Boolean).join(", "), a.district, a.city]
    .filter(Boolean).join(" - ") || "—";
  for (const [label, value] of [["OS", os.code || "—"], ["Endereço", address],
                                ["Status", OS_STATUS_LABEL[os.status] || os.status || "—"],
                                ["Duração", ""]]) {
    const cell = document.createElement("div");
    cell.className = "tech-detail-cell";
    const l = document.createElement("span"); l.className = "lbl"; l.textContent = label;
    const v = document.createElement("span"); v.className = "val";
    cell.append(l, v);
    if (label === "Duração") fillDuration(v, cell, os);
    else v.textContent = value;
    box.append(cell);
  }
  return box;
}

// 95 -> "1h35" ; 42 -> "42min" ; null/0 -> "—" (sem OS concluída ainda, média
// não significa nada).
function formatDuration(minutes) {
  if (!minutes) return "—";
  const m = Math.round(minutes);
  if (m < 60) return `${m}min`;
  return `${Math.floor(m / 60)}h${String(m % 60).padStart(2, "0")}`;
}

// Acordeão de cidades ("Atibaia .... 12"): `ordersByCity` já vem pronto e
// ordenado do SGI, um item por cidade com `name`/`count`/`percentage`.
// É PAREDE, ninguém clica: a rotação é automática, uma cidade por ciclo de
// atualização (mesmo `refreshSeconds` do resto da tela) — `cityRotationIndex`
// avança aqui, uma vez por chamada, e essa função só é chamada uma vez por
// ciclo (via refreshSecondary -> renderCompletion).
function renderCities(rows, advance = true) {
  const container = $("sec-cities");
  // Guarda o detalhe já desenhado de cada cidade: a lista é recriada a cada
  // ciclo, e sem isso a cidade aberta piscava (fecha, "Carregando…", reabre).
  const previous = new Map();
  container.querySelectorAll(".city").forEach((el) => {
    const d = el.querySelector(".city-detail");
    if (d && d.childNodes.length) previous.set(el.dataset.name, [...d.childNodes]);
  });
  container.innerHTML = "";
  for (const row of rows) {
    const name = row.name || "—";
    const count = row.count || 0;

    const wrap = document.createElement("div");
    wrap.className = "city";
    wrap.dataset.name = name;

    const header = document.createElement("div");
    header.className = "city-row";

    const nameEl = document.createElement("span");
    nameEl.className = "name";
    nameEl.textContent = name;

    const leader = document.createElement("span");
    leader.className = "leader";

    const countEl = document.createElement("span");
    countEl.className = "count";
    countEl.textContent = count;

    header.append(nameEl, leader, countEl);

    const detail = document.createElement("div");
    detail.className = "city-detail";
    if (previous.has(name)) detail.append(...previous.get(name));

    wrap.append(header, detail);
    container.appendChild(wrap);
  }

  if (rows.length === 1) {
    // Só 1 cidade: nada a rotacionar, fica aberta o tempo todo.
    cityRotationIndex = 0;
    openCity = rows[0].name;
  } else if (rows.length) {
    const keep = !advance && cityRotationIndex >= 0 && rows.some((r) => r.name === openCity);
    if (!keep) {
      cityRotationIndex = (cityRotationIndex + 1) % rows.length;
      openCity = rows[cityRotationIndex].name;
    }
  } else {
    cityRotationIndex = -1;
    openCity = null;
  }
  applyOpenCity();
  if (openCity) loadCityDetail(openCity);
}

function applyOpenCity() {
  $("sec-cities").querySelectorAll(".city").forEach((el) => {
    el.classList.toggle("open", el.dataset.name === openCity);
  });
}

/* O relatório não cruza cidade × categoria numa resposta só (`ordersByCity` e
   `ordersByCategory` são agregados independentes), então o detalhamento de
   dentro da cidade pede o resumo de novo, filtrado só naquela cidade
   (`?city=`), e lê o `ordersByCategory` dessa segunda resposta. */
let cityDetailToken = 0;   // descarta resposta de uma cidade que não é mais a aberta

async function loadCityDetail(name) {
  const meu = ++cityDetailToken;
  const el = $("sec-cities").querySelector(`.city[data-name="${CSS.escape(name)}"] .city-detail`);
  if (!el) return;
  if (!el.childNodes.length) el.innerHTML = '<span class="loading">Carregando…</span>';
  try {
    const r = await fetch(`/api/sgi/summary?city=${encodeURIComponent(name)}`,
      { headers: { Authorization: `Bearer ${token}` } });
    const body = await r.json();
    if (meu !== cityDetailToken) return;              // outra cidade foi aberta antes de voltar
    if (!r.ok) { el.textContent = body.detail || "Não foi possível carregar."; return; }
    renderCategoryBreakdown(el, name, body.ordersByCategory || [], meu);
  } catch (_) {
    if (meu === cityDetailToken) el.textContent = "Sem rede — tentando de novo…";
  }
}

// "Rompimento Massivo .... 2": mesmo desenho da linha da cidade, um nível
// abaixo. `ordersByCategory` já vem pronto e ordenado do SGI. Sob cada
// categoria entram os 3 status (ver renderStatusChips), buscados em paralelo
// — cada categoria pede o resumo de novo, filtrado por cidade + categoryId.
function renderCategoryBreakdown(container, city, rows, meu) {
  // Reaproveita os chips de status já exibidos até a resposta nova chegar,
  // pra a altura do card não pular a cada ciclo.
  const prevStatus = new Map();
  container.querySelectorAll(".category").forEach((el) => {
    const st = el.querySelector(".category-status");
    if (st && el.dataset.id) prevStatus.set(el.dataset.id, [...st.childNodes]);
  });
  container.innerHTML = "";
  if (!rows.length) {
    container.innerHTML = '<span class="loading">Sem categoria registrada.</span>';
    return;
  }
  const pendentes = rows.map((row) => {
    // Cada categoria é um mini-card (borda sutil, ver .category em app.css) —
    // não solta na lista: line+status ficam dentro do MESMO wrapper, que é o
    // item do grid de 2 colunas em .city-detail.
    const wrap = document.createElement("div");
    wrap.className = "category";

    const line = document.createElement("div");
    line.className = "category-row";

    const nameEl = document.createElement("span");
    nameEl.className = "name";
    nameEl.textContent = row.name || "—";

    const leader = document.createElement("span");
    leader.className = "leader";

    const countEl = document.createElement("span");
    countEl.className = "count";
    countEl.textContent = row.count || 0;

    line.append(nameEl, leader, countEl);

    const status = document.createElement("div");
    status.className = "category-status";
    if (prevStatus.has(String(row.id))) status.append(...prevStatus.get(String(row.id)));
    else status.innerHTML = '<span class="loading">…</span>';
    wrap.dataset.id = String(row.id);

    wrap.append(line, status);
    container.appendChild(wrap);
    return { id: row.id, status };
  });

  pendentes.forEach(({ id, status }) => {
    if (!id) { status.innerHTML = ""; return; }
    fetchCategoryStatus(city, id)
      .then((s) => { if (meu === cityDetailToken) renderStatusChips(status, s); })
      .catch(() => { if (meu === cityDetailToken) status.textContent = "—"; });
  });
}

async function fetchCategoryStatus(city, categoryId) {
  const r = await fetch(
    `/api/sgi/summary?city=${encodeURIComponent(city)}&category_id=${encodeURIComponent(categoryId)}`,
    { headers: { Authorization: `Bearer ${token}` } },
  );
  const body = await r.json();
  if (!r.ok) throw new Error(body.detail || `HTTP ${r.status}`);
  return body;
}

// Mesmas regras dos cards do título (ver renderCompletion), só que filtradas
// nesta cidade+categoria: "À fazer" = scheduled+rescheduled, "Em andamento" =
// inProgress, "Expiradas" = expired — passthrough, sem soma.
function renderStatusChips(container, s) {
  container.innerHTML = "";
  const chips = [
    ["À fazer", (s.scheduled || 0) + (s.rescheduled || 0)],
    ["Em andamento", s.inProgress || 0],
    ["Expiradas", s.expired || 0],
  ];
  for (const [label, value] of chips) {
    const chip = document.createElement("div");
    chip.className = "status-chip";

    const l = document.createElement("span");
    l.className = "label";
    l.textContent = label;

    const v = document.createElement("span");
    v.className = "value";
    v.textContent = value;

    chip.append(l, v);
    container.appendChild(chip);
  }
}

/* ---------------- Botão Configurar (QR para o celular) ---------------- */
let cfgPoll = null;

function closeCfg() {
  $("cfg").classList.remove("show");
  clearInterval(cfgPoll); cfgPoll = null;
}

$("cfgbtn").addEventListener("click", async (e) => {
  e.preventDefault();
  if (!token) return startAuth();
  try {
    const r = await fetch("/api/config/handoff", {
      method: "POST", headers: { Authorization: `Bearer ${token}` },
    });
    if (!r.ok) throw new Error();
    const h = await r.json();
    $("cfgqr").src = h.qr_url;
    $("cfgurl").value = h.url;
    $("cfg").classList.add("show");
    // Fecha sozinho assim que o celular ler o QR: ninguém precisa voltar na TV
    // para clicar em "Fechar" (o botão fica só como saída manual).
    clearInterval(cfgPoll);
    cfgPoll = setInterval(async () => {
      try {
        const st = await (await fetch(`/api/config/handoff/${h.uuid}/status`)).json();
        if (st.status !== "pending") closeCfg();
      } catch (_) { /* rede: tenta no próximo ciclo */ }
    }, 2000);
  } catch (_) { /* ignora */ }
});
$("cfgclose").addEventListener("click", (e) => { e.preventDefault(); closeCfg(); });

/* ---------------- Copiar link (alternativa ao QR) ---------------- */
/* `navigator.clipboard` so existe em contexto seguro (https ou localhost). O
   ambiente de dev roda em http://<ip-do-tailnet>, onde ele e' undefined — por
   isso o fallback com execCommand, que ainda funciona fora de contexto seguro.
   Se os dois falharem, o campo continua selecionavel e o botao diz isso. */
async function copyField(field, button) {
  const text = field.value;
  if (!text) return;

  let ok = false;
  try {
    if (window.isSecureContext && navigator.clipboard) {
      await navigator.clipboard.writeText(text);
      ok = true;
    }
  } catch (_) { /* negado: cai no fallback */ }

  if (!ok) {
    try {
      field.focus();
      field.select();
      field.setSelectionRange(0, text.length);   // iOS ignora select() sozinho
      ok = document.execCommand("copy");
    } catch (_) { ok = false; }
  }

  button.textContent = ok ? "Copiado!" : "Selecione e copie";
  button.classList.toggle("ok", ok);
  clearTimeout(button._resetTimer);
  button._resetTimer = setTimeout(() => {
    button.textContent = "Copiar";
    button.classList.remove("ok");
  }, 2500);
}

$("authcopy").addEventListener("click", () => copyField($("authurl"), $("authcopy")));
$("cfgcopy").addEventListener("click", () => copyField($("cfgurl"), $("cfgcopy")));

/* ---------------- Auto-update do front ---------------- */
/* A TV fica meses com a mesma aba aberta e não tem teclado: publicar front novo
   dependia de alguém subir lá e recarregar. Aqui ela pergunta ao servidor qual
   é a versão do front e, quando muda, se recarrega sozinha. Roda fora do ciclo
   de dados de propósito — assim funciona até na tela do QR-code. */
const VERSION_EVERY_MS = 60000;
let myVersion = null;

async function checkVersion() {
  let version;
  try {
    ({ version } = await (await fetch("/api/version", { cache: "no-store" })).json());
  } catch (_) { return; }                 // sem rede: tenta no próximo ciclo
  if (!version) return;
  if (myVersion === null) { myVersion = version; return; }
  if (version === myVersion) return;
  // Não puxa o tapete de quem está com o QR de configuração aberto no celular.
  if ($("cfg").classList.contains("show")) return;
  stale("nova versão disponível — atualizando…");
  setTimeout(() => location.reload(), 1500);
}

checkVersion();
setInterval(checkVersion, VERSION_EVERY_MS);

/* ---------------- Relógio ---------------- */
setInterval(() => { $("clock").textContent = new Date().toLocaleTimeString("pt-BR"); }, 1000);

/* ---------------- Início ---------------- */
if (token) boot(); else startAuth();
