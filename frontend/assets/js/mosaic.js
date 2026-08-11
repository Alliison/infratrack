/* TrackInfra — mosaico da TV.
   - Autentica via QR-code (guarda só um token opaco do nosso backend).
   - Monta um grid automático de mapas Leaflet (um por carro ligado).
   - Recentraliza cada mapa na posição do carro a cada atualização. */

const TOKEN_KEY = "trackinfra_token";
let token = localStorage.getItem(TOKEN_KEY);
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
  $("auth").classList.add("show");
  stopFleet();
  const r = await fetch("/api/auth/session", { method: "POST" });
  const s = await r.json();
  $("qrimg").src = s.qr_url;
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

/* ---------------- Ciclo de dados ---------------- */
async function boot() {
  try {
    cfg = await (await fetch("/api/config")).json();
  } catch (_) {}
  refreshFleet();
  clearInterval(fleetTimer);
  fleetTimer = setInterval(refreshFleet, Math.max(3, cfg.refresh_seconds || 6) * 1000);
}

function stopFleet() { clearInterval(fleetTimer); fleetTimer = null; }

async function refreshFleet() {
  if (!token) return startAuth();
  // relê o mosaico salvo a cada ciclo — mudanças feitas no celular aplicam ao vivo
  try { cfg = await (await fetch("/api/config")).json(); } catch (_) {}
  let list;
  try {
    const r = await fetch("/api/fleet", { headers: { Authorization: `Bearer ${token}` } });
    if (r.status === 401) {
      // O backend religa sozinho no FullTrack, então 401 aqui é sessão perdida
      // de vez (restart do backend ou senha trocada). Tolera blips antes de
      // mandar a TV para o QR — ela não deve piscar por um erro passageiro.
      if (++authFails < AUTH_FAILS_MAX) return stale("reconectando…");
      localStorage.removeItem(TOKEN_KEY); token = null; authFails = 0;
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
}

/* ---------------- Grid + mapas ---------------- */
function gridDims(n) {
  if (n <= 0) return { rows: 0, cols: 0 };
  const cols = Math.ceil(Math.sqrt(n));
  const rows = Math.ceil(n / cols);
  return { rows, cols };
}

const OFFLINE_MSG = "Esse veículo não está mais rastreável em tempo real. " +
  "Para ver sua última localização acesse o FullTrack ou configure novamente.";

// Ícone de velocímetro (Material "speed")
const SPEEDO_SVG = `<svg viewBox="0 0 24 24" fill="currentColor"><path d="M20.38 8.57l-1.23 1.85a8 8 0 0 1-.22 7.58H5.07A8 8 0 0 1 15.58 6.85l1.85-1.23A10 10 0 0 0 3.35 19a2 2 0 0 0 1.72 1h13.85a2 2 0 0 0 1.74-1 10 10 0 0 0-.27-10.44z"/><path d="M10.59 15.41a2 2 0 0 0 2.83 0l5.66-8.49-8.49 5.66a2 2 0 0 0 0 2.83z"/></svg>`;

// Formata placa BR: ABC1234 -> ABC-1234; Mercosul (ABC1D23) fica como está.
function formatPlaca(p) {
  if (!p) return "";
  const s = p.toUpperCase().replace(/[^A-Z0-9]/g, "");
  return /^[A-Z]{3}[0-9]{4}$/.test(s) ? s.slice(0, 3) + "-" + s.slice(3) : s;
}

// Decide quais veículos entram no mosaico, ordenados (ligados primeiro).
function computeDisplayed(fleet) {
  const byId = new Map(fleet.map(v => [v.id, v]));
  const sel = new Set(cfg.selected_ids || []);
  let ids;
  if (sel.size) {
    ids = [...sel].filter(id => byId.has(id));
  } else {
    const ligados = fleet.filter(v => cfg.only_ligados === false || v.ignicao).map(v => v.id);
    ids = [...new Set([...ligados, ...tiles.keys()])].filter(id => byId.has(id));
  }
  ids.sort((a, b) => (byId.get(b).ignicao ? 1 : 0) - (byId.get(a).ignicao ? 1 : 0));
  return { byId, ids };
}

// Liga/desliga o rodízio de páginas conforme a config (sem reiniciar à toa).
function ensureRotation() {
  const on = !!cfg.rotativo;
  const secs = Math.max(3, cfg.rotate_seconds || 15);
  if (rotState.on === on && rotState.secs === secs) return;
  rotState = { on, secs };
  clearInterval(rotateTimer); rotateTimer = null;
  if (on) rotateTimer = setInterval(() => { page++; render(lastFleet); }, secs * 1000);
}

function render(fleet) {
  const mosaic = $("mosaic");
  const { byId, ids: allIds } = computeDisplayed(fleet);
  const live = allIds.filter(id => byId.get(id).ignicao).length;

  // paginação (modo rotativo)
  const rot = !!cfg.rotativo;
  const pageSize = Math.max(1, cfg.page_size || 9);
  const pages = rot ? Math.max(1, Math.ceil(allIds.length / pageSize)) : 1;
  if (page >= pages) page = 0;
  const ids = rot ? allIds.slice(page * pageSize, page * pageSize + pageSize) : allIds;

  const { rows, cols } = gridDims(rot && pages > 1 ? pageSize : ids.length);
  $("grid").textContent = ids.length ? `${rows}×${cols}` : "–";
  $("count").textContent = live;
  $("page").textContent = rot && pages > 1 ? `pág ${page + 1}/${pages}` : "";
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
}

function createTile(car) {
  const el = document.createElement("div");
  el.className = "tile";
  el.innerHTML = `
    <div class="map"></div>
    <div class="offline-msg"><div class="om-box">
      <div class="om-icon">📴</div>
      <div class="om-title"></div>
      <div class="om-text">${OFFLINE_MSG}</div>
    </div></div>
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

  const marker = L.marker([car.lat, car.lng], { icon: carIcon(0, car.ignicao) }).addTo(map);
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
  t.el.querySelector(".om-title").textContent = nome + " · desligado";
  t.el.querySelector(".spd-val").textContent = off ? "—" : Math.round(car.velocidade) + " km/h";
  t.el.querySelector(".meta").textContent =
    [car.modelo, car.motorista, car.data_gps].filter(Boolean).join(" · ");
}

// Ícone de carro (Material Design "directions_car"); verde ligado, cinza desligado.
function carIcon(on) {
  const color = on ? "#17cf74" : "#8aa0b3";
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
$("cfgbtn").addEventListener("click", async (e) => {
  e.preventDefault();
  if (!token) return startAuth();
  try {
    const r = await fetch("/api/config/handoff", {
      method: "POST", headers: { Authorization: `Bearer ${token}` },
    });
    if (!r.ok) throw new Error();
    const s = await r.json();
    $("cfgqr").src = s.qr_url;
    $("cfg").classList.add("show");
  } catch (_) { /* ignora */ }
});
$("cfgclose").addEventListener("click", (e) => {
  e.preventDefault();
  $("cfg").classList.remove("show");
});

/* ---------------- Relógio ---------------- */
setInterval(() => { $("clock").textContent = new Date().toLocaleTimeString("pt-BR"); }, 1000);

/* ---------------- Início ---------------- */
if (token) boot(); else startAuth();
