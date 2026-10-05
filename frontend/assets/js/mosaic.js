/* TrackInfra — mosaico da TV.
   - Autentica via QR-code (guarda só um token opaco do nosso backend).
   - Monta um grid automático de mapas Leaflet (um por carro ligado).
   - Recentraliza cada mapa na posição do carro a cada atualização. */

const TOKEN_KEY = TI.tokenKey;      // token da TV (ou do celular, no espelho)
let token = TI.ls.get(TOKEN_KEY);
let cfg = { only_ligados: true, selected_ids: [], zoom: 15, refresh_seconds: 6 };
const tiles = new Map();   // id -> { map, marker, el, prev:[lat,lng] }
let fleetTimer = null;
let lastFleet = [];        // última frota recebida (para o rodízio re-renderizar)
let page = 0;              // página atual no modo rotativo
let rotateTimer = null;
let rotState = { on: null, secs: null };
let authFails = 0;                 // 401 seguidos; só o limite abaixo volta ao QR
const AUTH_FAILS_MAX = 5;

const $ = (id) => document.getElementById(id);

/* Erro passageiro: mantém o mosaico na tela e só avisa no rodapé. */
function stale(msg) { $("updated").textContent = msg; }

/* ---------------- Autenticação por QR ---------------- */
async function startAuth() {
  stopFleet();
  // Espelho no celular: sem QR — o token é o de controle e quem loga é a TV.
  if (TI.watch) return stale("acesso do celular expirado — abra pelo painel de TVs");
  $("auth").classList.add("show");
  const s = await TI.newQr();
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
      TI.authorized(s);
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
async function fetchCfg() {
  const r = await fetch("/api/config" + TI.devParam(), { headers: TI.auth(token) });
  if (r.ok) cfg = await r.json();
}

async function boot() {
  TI.learnDevice(token);
  try { await fetchCfg(); } catch (_) {}
  refreshFleet();
  clearInterval(fleetTimer);
  fleetTimer = setInterval(refreshFleet, Math.max(3, cfg.refresh_seconds || 6) * 1000);
}

function stopFleet() { clearInterval(fleetTimer); fleetTimer = null; }

async function refreshFleet() {
  if (!token) return startAuth();
  // relê o mosaico salvo a cada ciclo — mudanças feitas no celular aplicam ao vivo
  try { await fetchCfg(); } catch (_) {}
  // O SGI só entra no rodízio se estiver de fato configurado (ligado no
  // backend agora — a flag salva sobrevive a restart, o login não).
  try { sgiReady = !!(await (await fetch("/api/sgi/status")).json()).enabled; }
  catch (_) { /* mantém o último valor */ }
  let list;
  try {
    const r = await fetch("/api/fleet", { headers: TI.auth(token) });
    if (r.status === 401) {
      // O backend religa sozinho no FullTrack, então 401 aqui é sessão perdida
      // de vez (restart do backend ou senha trocada). Tolera blips antes de
      // mandar a TV para o QR — ela não deve piscar por um erro passageiro.
      // Desconectada pelo celular (header de revogação): QR na hora.
      if (!TI.revoked(r) && ++authFails < AUTH_FAILS_MAX) return stale("reconectando…");
      authFails = 0;
      if (TI.watch) { token = null; return startAuth(); }   // não apaga o token do celular por um espelho
      TI.ls.del(TOKEN_KEY); token = null;
      return startAuth();
    }
    if (!r.ok) return stale("FullTrack indisponível — tentando de novo…");
    list = await r.json();
  } catch (_) { return stale("sem rede — tentando de novo…"); }
  authFails = 0;

  lastFleet = list.filter(v => v.lat != null && v.lng != null);
  ensureRotation();
  render(lastFleet);
  $("updated").textContent = "atualizado " + new Date().toLocaleTimeString("pt-BR");
  TI.heartbeat(token, liveState);
}

/* ---------------- Grid + mapas ---------------- */
function gridDims(n) {
  if (n <= 0) return { rows: 0, cols: 0 };
  const cols = Math.ceil(Math.sqrt(n));
  const rows = Math.ceil(n / cols);
  return { rows, cols };
}

// Ícone de velocímetro (Material "speed")
const SPEEDO_SVG = `<svg viewBox="0 0 24 24" fill="currentColor"><path d="M20.38 8.57l-1.23 1.85a8 8 0 0 1-.22 7.58H5.07A8 8 0 0 1 15.58 6.85l1.85-1.23A10 10 0 0 0 3.35 19a2 2 0 0 0 1.72 1h13.85a2 2 0 0 0 1.74-1 10 10 0 0 0-.27-10.44z"/><path d="M10.59 15.41a2 2 0 0 0 2.83 0l5.66-8.49-8.49 5.66a2 2 0 0 0 0 2.83z"/></svg>`;

// Formata placa BR: ABC1234 -> ABC-1234; Mercosul (ABC1D23) fica como está.
function formatPlaca(p) {
  if (!p) return "";
  const s = p.toUpperCase().replace(/[^A-Z0-9]/g, "");
  return /^[A-Z]{3}[0-9]{4}$/.test(s) ? s.slice(0, 3) + "-" + s.slice(3) : s;
}

// Grades fixas que o modal do celular oferece; fora delas, grade automática.
const GRIDS = { "2x2": { rows: 2, cols: 2 }, "2x3": { rows: 2, cols: 3 } };

// Decide quais veículos entram no mosaico e em que ordem.
function computeDisplayed(fleet) {
  const byId = new Map(fleet.map(v => [v.id, v]));
  const sel = cfg.selected_ids || [];
  let ids;
  if (sel.length) {
    ids = sel.filter(id => byId.has(id));
  } else {
    const ligados = fleet.filter(v => cfg.only_ligados === false || v.ignicao).map(v => v.id);
    ids = [...new Set([...ligados, ...tiles.keys()])].filter(id => byId.has(id));
  }
  // Com sequência manual, a ordem de selected_ids é a que a pessoa montou no
  // celular; jogar os ligados para a frente destruiria justamente isso.
  if (!cfg.sequencia_manual) {
    ids.sort((a, b) => (byId.get(b).ignicao ? 1 : 0) - (byId.get(a).ignicao ? 1 : 0));
  }
  return { byId, ids };
}

// Liga/desliga o rodízio de páginas conforme a config (sem reiniciar à toa).
function ensureRotation() {
  const on = !!cfg.rotativo;
  const secs = Math.max(3, cfg.rotate_seconds || 15);
  if (rotState.on === on && rotState.secs === secs) return;
  rotState = { on, secs };
  clearInterval(rotateTimer); rotateTimer = null;
  if (on) rotateTimer = setInterval(() => {
    page++; render(lastFleet);
    TI.heartbeat(token, liveState);   // o painel do celular acompanha a troca de página
  }, secs * 1000);
}

// Página extra do rodízio: a tela do SGI, depois da última página de veículos.
let sgiReady = false;

let sgiShown = false;
let sgiLocked = false;

// Avisa a tela do SGI se está na vista e se está travada: ela só avança a
// rotação de cidades/técnicos por visita (ou a cada atualização, quando
// travada) — ver `visible`/`locked` em secondary.js.
function pushSgiVisibility() {
  try { $("sgi-frame").contentWindow.postMessage(
    { sgiVisible: sgiShown, sgiLocked }, location.origin); }
  catch (_) { /* iframe ainda sem documento */ }
}
$("sgi-frame").addEventListener("load", pushSgiVisibility);

function showSgiPage(on, locked = false) {
  const f = $("sgi-frame");
  if (on && !f.src) f.src = "/secondary" + TI.passQuery;
  f.classList.toggle("on", on);
  if (on !== sgiShown || locked !== sgiLocked) {
    sgiShown = on; sgiLocked = locked; pushSgiVisibility();
  }
}

// O que está na tela agora — vai no heartbeat para o painel do celular.
let liveState = { screen: "fulltrack", page: "", showing: [], grid: "" };

function render(fleet) {
  const mosaic = $("mosaic");
  const { byId, ids: allIds } = computeDisplayed(fleet);
  const live = allIds.filter(id => byId.get(id).ignicao).length;

  // paginação (modo rotativo)
  const rot = !!cfg.rotativo;
  // Grade fixa manda no tamanho da página: 2×2 são 4 telas, 2×3 são 6. Sem ela,
  // vale o page_size e a grade sai do número de carros.
  const fixo = GRIDS[cfg.grid];
  const pageSize = fixo ? fixo.rows * fixo.cols : Math.max(1, cfg.page_size || 9);
  const pages = rot ? Math.max(1, Math.ceil(allIds.length / pageSize)) : 1;
  // Com o SGI no rodízio ele entra ENTRE os grids: grid 1, SGI, grid 2, SGI...
  // `step` é a posição na sequência; o grid mostrado é step/2 e os passos
  // ímpares são a tela do SGI.
  const lock = cfg.screen_lock || "none";
  // Travado no SGI: só ele na tela, sem depender do rodízio; o SGI segue
  // girando cidades/técnicos por dentro. Sem SGI configurado, cai no mosaico.
  if (lock === "sgi" && sgiReady) {
    showSgiPage(true, true);
    $("page").textContent = "SGI · travado";
    liveState = { screen: "sgi", page: "SGI · travado", showing: [], grid: "" };
    return;
  }
  // Travado no FullTrack: o SGI não entra, os grids seguem girando.
  const sgiOn = rot && !!cfg.sgi_in_rotation && sgiReady && lock !== "fulltrack";
  const steps = sgiOn ? pages * 2 : pages;
  if (page >= steps) page = 0;
  if (sgiOn && page % 2 === 1) {
    showSgiPage(true);
    $("page").textContent = `pág ${page + 1}/${steps} · SGI`;
    liveState = { screen: "sgi", page: $("page").textContent, showing: [], grid: "" };
    return;
  }
  showSgiPage(false);
  const gridPage = sgiOn ? page / 2 : page;
  const ids = rot ? allIds.slice(gridPage * pageSize, gridPage * pageSize + pageSize)
                  : allIds.slice(0, fixo ? pageSize : allIds.length);

  const { rows, cols } = fixo || gridDims(rot && pages > 1 ? pageSize : ids.length);
  $("grid").textContent = ids.length ? `${rows}×${cols}` : "–";
  $("count").textContent = live;
  $("page").textContent = (rot && steps > 1 ? `pág ${page + 1}/${steps}` : "")
    + (lock === "fulltrack" ? (rot && steps > 1 ? " · " : "") + "FullTrack travado" : "")
    // Travada no SGI com o SGI fora do ar: cai no mosaico, mas diz isso — sem
    // o aviso parece que a trava simplesmente não funciona.
    + (lock === "sgi" ? (rot && steps > 1 ? " · " : "") + "SGI travado, mas desconectado" : "");
  mosaic.style.gridTemplateColumns = `repeat(${cols || 1}, 1fr)`;
  mosaic.style.gridTemplateRows = `repeat(${rows || 1}, 1fr)`;

  const wanted = new Set(ids);
  for (const [id, t] of tiles) {
    if (!wanted.has(id)) { t.map.remove(); t.el.remove(); tiles.delete(id); }
  }
  for (const id of ids) {
    let t = tiles.get(id) || createTile(byId.get(id));
    updateTile(t, byId.get(id));
  }
  // células vazias para completar o grid (ex.: 3 -> 2×2)
  const needed = Math.max(0, rows * cols - ids.length);
  mosaic.querySelectorAll(".tile.empty").forEach((e, i) => { if (i >= needed) e.remove(); });
  for (let i = mosaic.querySelectorAll(".tile.empty").length; i < needed; i++) {
    const e = document.createElement("div");
    e.className = "tile empty"; e.textContent = "—";
    mosaic.appendChild(e);
  }
  ids.forEach(id => { const t = tiles.get(id); if (t) mosaic.appendChild(t.el); });
  mosaic.querySelectorAll(".tile.empty").forEach(e => mosaic.appendChild(e));

  setTimeout(() => tiles.forEach(t => t.map.invalidateSize()), 60);
  liveState = {
    screen: "fulltrack", page: $("page").textContent, grid: $("grid").textContent,
    showing: ids.map(id => formatPlaca(byId.get(id).placa) || byId.get(id).modelo || id),
  };
}

function createTile(car) {
  const el = document.createElement("div");
  el.className = "tile";
  el.innerHTML = `
    <div class="map"></div>
    <div class="label">
      <span class="ign"></span>
      <span class="placa"></span>
      <span class="meta"></span>
      <span class="spd">${SPEEDO_SVG}<span class="spd-val"></span></span>
    </div>`;
  $("mosaic").appendChild(el);

  const map = L.map(el.querySelector(".map"), {
    zoomControl: false, attributionControl: false,
    dragging: false, scrollWheelZoom: false, doubleClickZoom: false,
    boxZoom: false, keyboard: false, touchZoom: false,
  }).setView([car.lat, car.lng], cfg.zoom || 15);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 19 }).addTo(map);

  const marker = L.marker([car.lat, car.lng], { icon: carIcon(car.ignicao) }).addTo(map);
  const t = { map, marker, el, prev: [car.lat, car.lng] };
  tiles.set(car.id, t);
  return t;
}

function updateTile(t, car) {
  const pos = [car.lat, car.lng];
  const off = !car.ignicao;
  t.el.classList.toggle("offline", off);

  if (!off) t.prev = pos;
  t.marker.setLatLng(pos);
  t.marker.setIcon(carIcon(car.ignicao));
  t.map.setView(pos, cfg.zoom || 15, { animate: true });   // recentraliza sempre

  t.el.querySelector(".ign").className = "ign" + (car.ignicao ? " on" : "");
  const nome = formatPlaca(car.placa) || car.modelo || car.id;
  t.el.querySelector(".placa").textContent = nome;
  t.el.querySelector(".spd-val").textContent = off ? "—" : Math.round(car.velocidade) + " km/h";
  t.el.querySelector(".meta").textContent =
    [car.modelo, car.motorista, car.data_gps].filter(Boolean).join(" · ");
}

// Ícone de carro (Material Design "directions_car"); verde ligado, vermelho
// desligado — é o que sinaliza o veículo parado, já que o tile não traz mais
// mensagem. O mapa esmaecido em cinza continua sendo o segundo sinal.
function carIcon(on) {
  const color = on ? "#17cf74" : "#ef4444";
  return L.divIcon({
    className: "",
    html: `<div class="car-mark">
      <svg width="34" height="34" viewBox="0 0 30 30">
        <circle cx="15" cy="15" r="13.5" fill="#fff" stroke="#04121b" stroke-width="1"/>
        <path transform="translate(3 3)" fill="${color}" d="M18.92 6.01C18.72 5.42 18.16 5 17.5 5h-11c-.66 0-1.21.42-1.42 1.01L3 12v8c0 .55.45 1 1 1h1c.55 0 1-.45 1-1v-1h12v1c0 .55.45 1 1 1h1c.55 0 1-.45 1-1v-8l-2.08-5.99zM6.5 16c-.83 0-1.5-.67-1.5-1.5S5.67 13 6.5 13s1.5.67 1.5 1.5S7.33 16 6.5 16zm11 0c-.83 0-1.5-.67-1.5-1.5s.67-1.5 1.5-1.5 1.5.67 1.5 1.5-.67 1.5-1.5 1.5zM5 11l1.5-4.5h11L19 11H5z"/>
      </svg></div>`,
    iconSize: [34, 34], iconAnchor: [17, 17],
  });
}

function bearing(a, b) {
  if (!a || a[0] === b[0] && a[1] === b[1]) return 0;
  const toRad = d => d * Math.PI / 180, toDeg = r => r * 180 / Math.PI;
  const dLon = toRad(b[1] - a[1]);
  const y = Math.sin(dLon) * Math.cos(toRad(b[0]));
  const x = Math.cos(toRad(a[0])) * Math.sin(toRad(b[0])) -
            Math.sin(toRad(a[0])) * Math.cos(toRad(b[0])) * Math.cos(dLon);
  return (toDeg(Math.atan2(y, x)) + 360) % 360;
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
  if (TI.watch) return;
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
// Espelho: o botão "Configurar" é da TV física, não do celular que a observa.
if (TI.watch) { $("cfgbtn").hidden = true; document.body.classList.add("watch"); }
if (token) boot(); else startAuth();
