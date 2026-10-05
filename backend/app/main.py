import asyncio
import contextlib
import hashlib
import io
import time
from pathlib import Path
from typing import Optional

import httpx
import qrcode
from fastapi import Body, Depends, FastAPI, HTTPException, Header, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import devices, fulltrack, sgi, storage
from . import persist
from .config import settings
from .models import (CommandRequest, LoginRequest, MosaicConfig, PairRequest, RenameRequest,
                     SessionRequest, SessionResponse, SgiEnableRequest, StatusResponse,
                     Vehicle)
from .sessions import AppSession, store

@contextlib.asynccontextmanager
async def lifespan(_: FastAPI):
    """Sobe o health check do token do SGI junto com o app.

    Roda mesmo com o SGI desabilitado: o loop checa `is_enabled()` a cada ciclo,
    entao habilitar pela tela nao precisa reiniciar nada.
    """
    await sgi.autoenable_from_env()
    await persist.restore()
    task = asyncio.create_task(sgi.health_loop())
    save_task = asyncio.create_task(persist.save_loop())
    try:
        yield
    finally:
        persist.save(force=True)
        for t in (task, save_task):
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await t


app = FastAPI(title="TrackInfra", version="1.0.0", lifespan=lifespan)

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"


def _asset_version() -> str:
    """Impressao digital do front servido.

    E' o que permite a TV se atualizar sozinha: ela guarda esta string no load e
    recarrega quando ela muda. Derivada do CONTEUDO dos arquivos, nunca da hora
    de boot — com timestamp, todo restart do backend recarregaria os paineis a
    toa, e um deploy que nao mexeu no front nao deve mexer na tela de ninguem.

    Recalculada A CADA CHAMADA (nao guardada numa global calculada soh no
    import): em producao tanto faz, porque front so muda com um restart. Mas
    no dev o front eh lido do volume montado, sem restart nenhum do processo
    (--reload-dir soh cobre .py) — se isto fosse calculado uma vez soh, editar
    CSS/HTML/JS nunca mudaria a versao, e a TV de teste nunca recarregaria
    sozinha pra mostrar o que mudou.
    """
    h = hashlib.sha256()
    for p in sorted(FRONTEND.rglob("*")):
        if p.is_file():
            h.update(p.relative_to(FRONTEND).as_posix().encode())
            h.update(p.read_bytes())
    return h.hexdigest()[:12]


class NoCacheStatic(StaticFiles):
    """Assets com `Cache-Control: no-cache` (revalida sempre, 304 se igual).

    Sem isto o StaticFiles nao manda Cache-Control nenhum, e o navegador fica
    livre para servir do cache por heuristica. Numa TV que nunca se reinicia,
    isso significaria recarregar e continuar com o JS velho — o auto-update
    entraria em loop, achando que a versao nova nunca chega.
    """

    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


# ----- dependências de autenticação (tokens do nosso app) -----------------
async def current_session(authorization: str = Header(default="")) -> AppSession:
    token = authorization.removeprefix("Bearer ").strip()
    s = await store.get_app(token) if token else None
    if not s:
        # Derrubado de propósito (desconectar TV/tudo, senha trocada): a TV vai
        # direto para o QR. Sem o header, ela trata o 401 como blip e insiste.
        revoked = bool(token) and store.is_revoked(token)
        raise HTTPException(status_code=401,
                            detail="Sessão desconectada." if revoked else "Sessão inválida ou expirada.",
                            headers={"X-Session-Revoked": "1"} if revoked else None)
    return s


async def tv_session(s: AppSession = Depends(current_session)) -> AppSession:
    if s.kind != "tv":
        raise HTTPException(status_code=403, detail="Só uma TV pode fazer isto.")
    return s


async def controller_session(s: AppSession = Depends(current_session)) -> AppSession:
    if s.kind != "controller":
        raise HTTPException(status_code=403, detail="Abra pelo celular que gerencia as TVs.")
    return s


def device_for(s: AppSession, device_id: Optional[str] = None) -> devices.Device:
    """A TV só enxerga ela mesma; o celular, qualquer TV da conta. Sem `device`,
    o celular cai na TV de onde veio o QR "Configurar" (handoff)."""
    if s.kind == "tv":
        if device_id and device_id != s.device_id:
            raise HTTPException(status_code=403, detail="Esta TV não pode mexer em outra.")
        device_id = s.device_id
    device_id = device_id or s.device_id
    d = devices.get(device_id)
    if not d or d.account_id != s.account.id:
        raise HTTPException(status_code=404, detail="TV não encontrada nesta conta.")
    return d


def _ua_label(request: Request) -> str:
    """Descrição curta do celular para a lista de acessos ("Android · Chrome")."""
    ua = request.headers.get("user-agent", "")
    so = next((n for k, n in (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"),
                              ("Windows", "Windows"), ("Mac OS", "Mac"), ("Linux", "Linux"))
               if k in ua), "Navegador")
    nav = next((n for k, n in (("Edg/", "Edge"), ("SamsungBrowser", "Samsung"), ("Firefox", "Firefox"),
                               ("CriOS", "Chrome"), ("Chrome", "Chrome"), ("Safari", "Safari"))
                if k in ua), "")
    return f"{so} · {nav}" if nav else so


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


# ===== Fluxo de autenticação por QR-code ==================================
@app.post("/api/auth/session", response_model=SessionResponse)
async def create_session(body: Optional[SessionRequest] = Body(default=None)):
    """A TV chama isto quando não tem token; recebe o QR para exibir. Manda o
    `device_id` que lembra (se lembrar) para voltar a ser a mesma TV."""
    s = await store.create_qr(body.device_id if body else None)
    return SessionResponse(
        session_uuid=s.uuid,
        login_url=f"{settings.public_base_url}/login?s={s.uuid}",
        qr_url=f"/api/auth/qr/{s.uuid}.png",
        expires_in=settings.qr_session_ttl,
    )


@app.get("/api/auth/qr/{uuid}.png")
async def qr_png(uuid: str):
    url = f"{settings.public_base_url}/login?s={uuid}"
    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Response(buf.getvalue(), media_type="image/png",
                    headers={"Cache-Control": "no-store"})


async def _pending_qr(uuid: str):
    qr = await store.get_qr(uuid)
    if not qr or qr.status == "expired":
        raise HTTPException(status_code=410, detail="QR-code expirado. Gere um novo na TV.")
    if qr.status != "pending":
        raise HTTPException(status_code=409, detail="Esta TV já foi liberada.")
    return qr


@app.post("/api/auth/login")
async def do_login(body: LoginRequest, request: Request):
    """O celular envia as credenciais do FullTrack e libera a TV.

    Além de liberar a TV, o celular sai daqui como CONTROLE da conta: recebe um
    token próprio para vincular as próximas TVs sem senha e gerenciar todas.
    Mesmo usuário do FullTrack = mesma conta, venha de qual TV vier.
    """
    qr = await _pending_qr(body.session_uuid)
    try:
        auth = await fulltrack.login(body.login, body.password)
    except fulltrack.AuthError as e:
        raise HTTPException(status_code=401, detail=str(e))
    account = await store.upsert_account(auth)
    dev = devices.claim(account.id, qr.device_id)
    if not await store.authorize_qr(body.session_uuid, account, dev.id):
        raise HTTPException(status_code=410, detail="QR-code expirado. Gere um novo na TV.")
    ctl = await store.new_controller(account, label=_ua_label(request))
    persist.save()
    return {"ok": True, "controller_token": ctl.app_token, "account": account.login_user,
            "device": {"id": dev.id, "name": dev.name}}


@app.post("/api/auth/pair")
async def pair(body: PairRequest, s: AppSession = Depends(controller_session)):
    """Celular já logado libera uma TV nova na MESMA conta — sem pedir a senha
    do FullTrack de novo. A TV usa a sessão do FullTrack que a conta já tem."""
    qr = await _pending_qr(body.session_uuid)
    dev = devices.claim(s.account.id, qr.device_id, (body.name or "").strip()[:60] or None)
    if not await store.authorize_qr(body.session_uuid, s.account, dev.id):
        raise HTTPException(status_code=410, detail="QR-code expirado. Gere um novo na TV.")
    persist.save()
    return {"ok": True, "device": {"id": dev.id, "name": dev.name}}


@app.get("/api/auth/status/{uuid}", response_model=StatusResponse)
async def auth_status(uuid: str):
    """A TV faz polling aqui até virar 'authorized'."""
    s = await store.get_qr(uuid)
    if not s:
        return StatusResponse(status="expired")
    ok = s.status == "authorized"
    return StatusResponse(status=s.status, access_token=s.app_token if ok else None,
                          device_id=s.device_id if ok else None)


@app.post("/api/auth/logout")
async def logout(session: AppSession = Depends(current_session)):
    await store.drop_app(session.app_token)
    return {"ok": True}


@app.get("/api/me")
async def me(s: AppSession = Depends(current_session)):
    """Quem é o dono deste token. O celular usa para saber se ainda é controle
    (e de qual conta) antes de oferecer "liberar sem senha"."""
    d = devices.get(s.device_id)
    return {"kind": s.kind, "account": s.account.login_user,
            "device_id": s.device_id, "device_name": d.name if d else None}


# ===== Dispositivos (gestão pelo celular) ================================
ONLINE_S = 45   # sem heartbeat há mais que isto = TV fora do ar (aba fechada, sem rede)


def _device_view(d: devices.Device, now: float) -> dict:
    lv = devices.live(d.id)
    connected = store.device_connected(d.id)
    return {
        "id": d.id, "name": d.name, "created_at": d.created_at,
        "connected": connected,
        "online": connected and now - lv.last_seen < ONLINE_S,
        "last_seen": lv.last_seen or None,
        "ip": lv.ip, "user_agent": lv.user_agent, "screen": lv.screen, "page": lv.page,
        "showing": lv.showing, "grid": lv.grid, "viewport": lv.viewport,
        "config": d.config.model_dump(),
    }


@app.get("/api/devices")
async def list_devices(s: AppSession = Depends(controller_session)):
    now = time.time()
    ctls = [{
        "id": g.id, "label": g.label or "Celular", "created_at": g.created_at,
        "last_used_at": g.last_used_at, "current": g.app_token == s.app_token,
        "expires_in": (g.idle_ttl - (now - g.last_used_at)) if g.idle_ttl else None,
    } for g in store.grants_of(s.account.id) if g.kind == "controller"]
    return {"account": s.account.login_user,
            # A trava "Só SGI" depende disto: sem SGI conectado a TV mostra o
            # FullTrack, e o painel precisa dizer por quê.
            "sgi_enabled": sgi.is_enabled(),
            "devices": [_device_view(d, now) for d in devices.of_account(s.account.id)],
            "controllers": sorted(ctls, key=lambda c: c["created_at"])}


@app.patch("/api/devices/{device_id}")
async def rename_device(device_id: str, body: RenameRequest,
                        s: AppSession = Depends(controller_session)):
    d = device_for(s, device_id)
    devices.rename(d.id, body.name)
    return {"ok": True, "name": devices.get(d.id).name}


@app.post("/api/devices/{device_id}/disconnect")
async def disconnect_device(device_id: str, s: AppSession = Depends(controller_session)):
    """Derruba só esta TV (ela volta ao QR). Nome e configuração ficam: liberar
    a mesma TV de novo devolve tudo como estava."""
    d = device_for(s, device_id)
    n = await store.drop_device(s.account.id, d.id)
    persist.save()
    return {"ok": True, "dropped": n}


@app.delete("/api/devices/{device_id}")
async def forget_device(device_id: str, s: AppSession = Depends(controller_session)):
    """Desconecta e apaga o registro (nome/config) da TV."""
    d = device_for(s, device_id)
    await store.drop_device(s.account.id, d.id)
    devices.forget(d.id)
    persist.save()
    return {"ok": True}


@app.post("/api/devices/{device_id}/command")
async def device_command(device_id: str, body: CommandRequest,
                         s: AppSession = Depends(controller_session)):
    """Comando entregue no próximo heartbeat da TV (identificar, recarregar)."""
    d = device_for(s, device_id)
    devices.push_command(d.id, body.cmd)
    return {"ok": True}


@app.post("/api/account/disconnect-all")
async def disconnect_all(s: AppSession = Depends(controller_session)):
    """Todas as TVs e todos os celulares da conta caem, inclusive este. A
    sessão do FullTrack sai da memória; relogar devolve cada TV como estava."""
    n = await store.drop_account(s.account.id)
    persist.save()
    return {"ok": True, "dropped": n}


@app.delete("/api/controllers/{grant_id}")
async def revoke_controller(grant_id: str, s: AppSession = Depends(controller_session)):
    g = next((g for g in store.grants_of(s.account.id)
              if g.id == grant_id and g.kind == "controller"), None)
    if not g:
        raise HTTPException(status_code=404, detail="Acesso não encontrado.")
    await store.drop_app(g.app_token)
    persist.save()
    return {"ok": True}


@app.post("/api/devices/me/heartbeat")
async def heartbeat(request: Request, data: dict = Body(default={}),
                    s: AppSession = Depends(tv_session)):
    """A TV conta o que está mostrando; a resposta leva comandos pendentes e o
    nome dela (para o "Identificar")."""
    d = device_for(s)
    cmds = devices.heartbeat(d.id, data, _client_ip(request),
                             request.headers.get("user-agent", ""))
    return {"commands": cmds, "name": d.name}


# ===== Dados =============================================================
# Várias TVs da mesma conta pedem a frota no mesmo ritmo; sem isto cada uma
# viraria uma chamada ao FullTrack. Janela curta: a posição continua "ao vivo".
FLEET_CACHE_S = 2.0
_fleet_cache: dict[str, tuple[float, list[Vehicle]]] = {}
_fleet_locks: dict[str, asyncio.Lock] = {}


async def _fleet_of(account) -> list[Vehicle]:
    lock = _fleet_locks.setdefault(account.id, asyncio.Lock())
    async with lock:
        hit = _fleet_cache.get(account.id)
        if hit and time.time() - hit[0] < FLEET_CACHE_S:
            return hit[1]
        data = await fulltrack.get_fleet(account.auth)
        _fleet_cache[account.id] = (time.time(), data)
        return data


@app.get("/api/fleet", response_model=list[Vehicle])
async def fleet(session: AppSession = Depends(current_session)):
    """A sessão da TV não cai por instabilidade do FullTrack: o cliente já refaz
    o login sozinho. Só credencial que deixou de valer (senha trocada) devolve
    401 — e aí a CONTA inteira cai, porque todas as TVs dividem essa senha."""
    try:
        return await _fleet_of(session.account)
    except fulltrack.AuthError as e:
        await store.drop_account(session.account.id)
        persist.save()
        raise HTTPException(status_code=401, detail=str(e), headers={"X-Session-Revoked": "1"})
    except (fulltrack.SessionExpired, httpx.HTTPError):
        raise HTTPException(status_code=503, detail="FullTrack indisponível no momento.")


@app.get("/api/notifications/total")
async def notifications_total(session: AppSession = Depends(current_session)):
    try:
        return {"total_unread": await fulltrack.get_notifications_total(session.auth)}
    except httpx.HTTPError:
        return {"total_unread": 0}


# ===== Configuração (por TV) =============================================
# Cada TV tem a sua. A TV lê/grava a própria; o celular passa `?device=<id>`.
@app.get("/api/config", response_model=MosaicConfig)
async def get_config(device: Optional[str] = None, s: AppSession = Depends(current_session)):
    return device_for(s, device).config


@app.post("/api/config", response_model=MosaicConfig)
async def set_config(cfg: MosaicConfig, device: Optional[str] = None,
                     s: AppSession = Depends(current_session)):
    d = device_for(s, device)
    # Só deixa LIGAR o que depende do SGI com o SGI conectado. Quem já estava
    # ligado continua passando: se o SGI cair depois, salvar outra coisa (a
    # seleção de carros, por exemplo) não pode ser recusado por causa disso.
    if not sgi.is_enabled():
        if cfg.screen_lock == "sgi" and d.config.screen_lock != "sgi":
            raise HTTPException(status_code=409,
                                detail="Conecte o SGI antes de travar a TV nele.")
        if cfg.sgi_in_rotation and not d.config.sgi_in_rotation:
            raise HTTPException(status_code=409,
                                detail="Conecte o SGI antes de incluí-lo no rodízio.")
    return devices.set_config(d.id, cfg)


# ----- Configurar pelo celular via QR (handoff) --------------------------
@app.post("/api/config/handoff")
async def config_handoff(session: AppSession = Depends(tv_session)):
    """A TV pede um QR para configurar no celular (token curto e de uso único)."""
    h = await store.create_handoff(session)
    return {
        "uuid": h.uuid,
        "url": f"{settings.public_base_url}/config?c={h.uuid}",
        "qr_url": f"/api/config/handoff/{h.uuid}.png",
        "expires_in": settings.qr_session_ttl,
    }


@app.get("/api/config/handoff/{uuid}.png")
async def config_handoff_qr(uuid: str):
    url = f"{settings.public_base_url}/config?c={uuid}"
    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Response(buf.getvalue(), media_type="image/png",
                    headers={"Cache-Control": "no-store"})


@app.get("/api/config/handoff/{uuid}/status")
async def config_handoff_status(uuid: str):
    """A TV faz polling aqui enquanto exibe o QR de configuracao e fecha o
    overlay sozinha quando o celular le'. Numa TV de parede, depender de alguem
    achar o mouse para clicar em "Fechar" e' o que se quer evitar.

    Sumiu do store = expirou (o _gc leva embora depois do qr_session_ttl); para
    a TV da' no mesmo que "usado": nos dois casos o QR na tela nao serve mais.
    """
    h = await store.get_handoff(uuid)
    if not h:
        return {"status": "expired"}
    return {"status": "used" if h.used else "pending"}


@app.post("/api/config/handoff/{uuid}/redeem")
async def config_handoff_redeem(uuid: str, request: Request):
    """O celular troca o UUID do QR por um token de controle curto, já
    apontado para a TV que mostrou o QR."""
    s = await store.redeem_handoff(uuid, label=_ua_label(request) + " (QR da TV)")
    if not s:
        raise HTTPException(status_code=410, detail="QR de configuração expirado ou já usado.")
    return {"access_token": s.app_token, "device_id": s.device_id,
            "account": s.account.login_user}


# ===== SGI (tela secundária) =============================================
# A conexão com o SGI é UMA só (painel de uma empresa, ver sgi.py). O que é de
# cada TV é o filtro: equipe e tipos de OS vêm da configuração dela.
@app.get("/api/sgi/status")
async def sgi_status():
    """Estado do SGI para a tela. Sem autenticação: não devolve dado nenhum do
    SGI, só se está ligado — e a tela de configuração precisa disso para saber
    qual formulário mostrar antes de qualquer token."""
    st = sgi.status()
    st["base_url"] = settings.sgi_base_url
    # Intenção salva (sobrevive a restart; a credencial, sem persistência, não).
    st["was_enabled"] = storage.load_config().sgi_enabled
    return st


@app.post("/api/sgi/enable")
async def sgi_enable(body: SgiEnableRequest, session: AppSession = Depends(current_session)):
    """Habilita o SGI com a credencial que o usuário digitou no celular.

    Exige sessão do FullTrack: quem configura é quem já provou ter acesso ao
    painel. A senha do SGI fica só em RAM (ver sgi.py) — o que é persistido em
    disco é apenas o flag `sgi_enabled`.
    """
    try:
        st = await sgi.enable(body.username.strip(), body.password, body.totp_secret)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    cfg = storage.load_config()
    cfg.sgi_enabled = True
    storage.save_config(cfg)
    return st


@app.post("/api/sgi/disable")
async def sgi_disable(session: AppSession = Depends(current_session)):
    st = await sgi.disable()
    cfg = storage.load_config()
    cfg.sgi_enabled = False
    storage.save_config(cfg)
    return st


@app.get("/api/sgi/summary")
async def sgi_summary(city: Optional[str] = None, category_id: Optional[str] = None,
                       device: Optional[str] = None,
                       session: AppSession = Depends(current_session)):
    """Resumo de ordens de serviço de HOJE. Painel é real time: sem seletor de data.

    Equipe e tipo(s) de OS vêm da configuração da TV — nenhum filtro fixo no
    código. `city` e `category_id`, opcionais, descem de nível na tela
    secundária: cidade -> categoria -> status (`scheduled`/`inProgress`/
    `expired`), cada um filtrando o resumo mais fundo que o de cima.
    """
    if not sgi.is_enabled():
        raise HTTPException(status_code=409, detail="SGI não habilitado.")
    cfg = device_for(session, device).config
    try:
        return await sgi.fetch_summary(team_id=cfg.sgi_team, types=cfg.sgi_types,
                                        cities=[city] if city else None,
                                        category_ids=[category_id] if category_id else None)
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=502,
                            detail=f"SGI respondeu {exc.response.status_code}.") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/sgi/technicians")
async def sgi_technicians(session: AppSession = Depends(current_session)):
    """Ficha dos técnicos ({id: {status, isAvailable, ...}}) — avatar e legenda do resumo."""
    if not sgi.is_enabled():
        raise HTTPException(status_code=409, detail="SGI não habilitado.")
    try:
        return await sgi.fetch_technicians()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=502,
                            detail=f"SGI respondeu {exc.response.status_code}.") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/sgi/teams")
async def sgi_teams(session: AppSession = Depends(current_session)):
    """Equipes para o seletor. Vêm do SGI, nunca de lista mantida aqui."""
    if not sgi.is_enabled():
        raise HTTPException(status_code=409, detail="SGI não habilitado.")
    try:
        return await sgi.fetch_teams()
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/sgi/discover")
async def sgi_discover(session: AppSession = Depends(current_session)):
    """Contrato do SGI conforme a spec OpenAPI que ele publica.

    Substituiu a antiga varredura por endpoints chutados: aqui a resposta vem da
    fonte de verdade, e mostra se o `teamId` do filtro de equipe ainda existe.
    """
    if not sgi.is_enabled():
        raise HTTPException(status_code=409, detail="SGI não habilitado.")
    return await sgi.discover()


# ===== Front (páginas) ===================================================
@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/api/version")
async def version():
    """Versao do front. A TV compara com a que carregou e se recarrega sozinha
    quando muda — e' o unico jeito de publicar front novo num painel de parede,
    que nao tem quem aperte F5."""
    return JSONResponse({"version": _asset_version()},
                        headers={"Cache-Control": "no-store"})


@app.get("/")
async def index():
    # no-cache tambem aqui: de nada adianta a TV recarregar se o proprio HTML
    # vier do cache apontando para os assets antigos.
    #
    # A tela do SGI agora entra no rodízio do próprio mosaico (iframe, ver
    # `sgi_in_rotation`), então `/` serve sempre o mosaico. O antigo flag
    # `secondary_screen_only` (dev: `/` só com o SGI) deixou de ter efeito.
    page = "index.html"
    return FileResponse(FRONTEND / page, headers={"Cache-Control": "no-cache"})


# Página do SGI em rota própria: o mosaico a embute (iframe) como mais uma página
# do rodízio, ver `sgi_in_rotation`. `/` não muda.
@app.get("/mosaic")
async def mosaic_page():
    return FileResponse(FRONTEND / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/secondary")
async def secondary_page():
    return FileResponse(FRONTEND / "secondary.html", headers={"Cache-Control": "no-cache"})


@app.get("/login")
async def login_page():
    return FileResponse(FRONTEND / "login.html")


@app.get("/config")
async def config_page():
    return FileResponse(FRONTEND / "config.html")


@app.get("/devices")
async def devices_page():
    return FileResponse(FRONTEND / "devices.html", headers={"Cache-Control": "no-cache"})


app.mount("/assets", NoCacheStatic(directory=FRONTEND / "assets"), name="assets")
