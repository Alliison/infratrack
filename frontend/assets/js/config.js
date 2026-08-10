/* Configuração do mosaico salvo.
   - No celular: aberto via QR com ?c=<uuid>, troca por um token curto (handoff).
   - Na TV (fallback): usa o token guardado no localStorage. */
const TOKEN_KEY = "trackinfra_token";
const $ = (id) => document.getElementById(id);
let token = null;
let fleet = [];
let selected = new Set();

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
  $("zoom").value = cfg.zoom;
  $("refresh").value = cfg.refresh_seconds;
  $("only_ligados").checked = cfg.only_ligados !== false;
  $("rotativo").checked = !!cfg.rotativo;
  $("rotate").value = cfg.rotate_seconds || 15;
  $("page_size").value = cfg.page_size || 9;

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
  selected = new Set(cfg.selected_ids || []);
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
    row.innerHTML = `
      <input type="checkbox" value="${v.id}" ${selected.has(v.id) ? "checked" : ""}>
      <div class="veh-info">
        <div class="veh-placa">${formatPlaca(v.placa) || v.id}</div>
        <div class="veh-modelo">${v.modelo || "—"}</div>
      </div>
      <span class="badge ${v.ignicao ? "on" : "off"}">${v.ignicao ? "Ligado" : "Desligado"}</span>`;
    row.querySelector("input").addEventListener("change", (e) => {
      if (e.target.checked) selected.add(v.id); else selected.delete(v.id);
      updateCount();
    });
    list.appendChild(row);
  }
  updateCount();
}

function updateCount() {
  $("selcount").textContent = selected.size ? `(${selected.size} selecionados)` : "(todos)";
}

$("clear").addEventListener("click", () => {
  selected.clear();
  renderList();
  $("msg").className = "msg";
  $("msg").textContent = "Seleção limpa. Selecione os veículos e salve.";
});

$("save").addEventListener("click", async () => {
  const cfg = {
    selected_ids: [...selected],
    only_ligados: $("only_ligados").checked,
    zoom: parseInt($("zoom").value, 10) || 15,
    refresh_seconds: parseInt($("refresh").value, 10) || 6,
    rotativo: $("rotativo").checked,
    rotate_seconds: parseInt($("rotate").value, 10) || 15,
    page_size: parseInt($("page_size").value, 10) || 9,
  };
  $("msg").className = "msg";
  $("msg").textContent = "Salvando…";
  const r = await fetch("/api/config", {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
    body: JSON.stringify(cfg),
  });
  if (r.ok) { $("msg").className = "msg ok"; $("msg").textContent = "Mosaico salvo! ✅"; }
  else { $("msg").className = "msg err"; $("msg").textContent = "Erro ao salvar."; }
});

init();
