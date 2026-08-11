/* Configuração do mosaico salvo.
   - No celular: aberto via QR com ?c=<uuid>, troca por um token curto (handoff).
   - Na TV (fallback): usa o token guardado no localStorage. */
const TOKEN_KEY = "trackinfra_token";
const $ = (id) => document.getElementById(id);
let token = null;
let fleet = [];
/* Array, não Set: a ORDEM em que os veículos são marcados é a sequência que o
   modal oferece. Um Set descartaria essa informação. */
let selected = [];
let serverCfg = {};        // o que está salvo hoje — base do "Seguir automático"
let escolhaGrid = "auto";

const CELULAS = { "2x2": 4, "2x3": 6 };

async function resolveToken() {
  const c = new URLSearchParams(location.search).get("c");
  if (c) {
    const r = await fetch(`/api/config/handoff/${c}/redeem`, { method: "POST" });
    if (!r.ok) return null;
    return (await r.json()).access_token;
  }
  return localStorage.getItem(TOKEN_KEY);
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
  let cfg = { selected_ids: [], only_ligados: true, zoom: 15, refresh_seconds: 6 };
  try { cfg = await (await fetch("/api/config")).json(); } catch (_) {}
  serverCfg = cfg;
  $("zoom").value = cfg.zoom;
  $("refresh").value = cfg.refresh_seconds;
  $("only_ligados").checked = cfg.only_ligados !== false;

  try {
    const r = await fetch("/api/fleet", { headers: { Authorization: `Bearer ${token}` } });
    if (r.status === 401) throw new Error("expired");
    fleet = await r.json();
  } catch (e) {
    $("list").innerHTML = "";
    $("msg").className = "msg err";
    $("msg").textContent = "Sessão expirada. Refaça o login na TV.";
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
function abrirConfirm() {
  escolhaGrid = serverCfg.grid || "auto";
  $("cf-rotativo").checked = !!serverCfg.rotativo;
  $("cf-rotate").value = serverCfg.rotate_seconds || 15;
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

async function salvar(cfg, botao) {
  $("msg").className = "msg";
  $("msg").textContent = "Salvando…";
  botao.disabled = true;
  try {
    const r = await fetch("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
      body: JSON.stringify(cfg),
    });
    if (!r.ok) throw new Error();
    serverCfg = cfg;
    fecharConfirm();
    $("msg").className = "msg ok";
    $("msg").textContent = "Mosaico salvo! ✅";
  } catch (_) {
    $("msg").className = "msg err";
    $("msg").textContent = "Erro ao salvar.";
  } finally {
    botao.disabled = false;
  }
}

// O que sempre vai junto, escolhendo layout ou não.
function base() {
  return {
    selected_ids: [...selected],
    only_ligados: $("only_ligados").checked,
    zoom: parseInt($("zoom").value, 10) || 15,
    refresh_seconds: parseInt($("refresh").value, 10) || 6,
  };
}

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
    rotativo: $("cf-rotativo").checked,
    rotate_seconds: parseInt($("cf-rotate").value, 10) || 15,
    page_size: fixo || parseInt($("cf-page-size").value, 10) || 9,
  }, $("cf-apply"));
});

/* Seguir automático: guarda os veículos marcados e devolve o layout ao
   comportamento de sempre (grade pelo número de carros, ligados na frente).
   Rodízio e tempos ficam como já estavam salvos — este botão existe para NÃO
   impor nada novo. */
$("cf-auto").addEventListener("click", (e) => {
  e.preventDefault();
  salvar({
    ...base(),
    grid: "auto",
    sequencia_manual: false,
    rotativo: !!serverCfg.rotativo,
    rotate_seconds: serverCfg.rotate_seconds || 15,
    page_size: serverCfg.page_size || 9,
  }, $("cf-auto"));
});

init();
