import asyncio
import contextlib
import hashlib
import io
from pathlib import Path
from typing import Optional

import httpx
import qrcode
from fastapi import Depends, FastAPI, HTTPException, Header
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import fulltrack, sgi, storage
from . import persist
from .config import settings
from .models import (LoginRequest, MosaicConfig, SessionResponse, SgiEnableRequest,
                     StatusResponse, Vehicle)
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


# ----- dependência de autenticação (token do nosso app) -------------------
async def current_session(authorization: str = Header(default="")) -> AppSession:
    token = authorization.removeprefix("Bearer ").strip()
    s = await store.get_app(token) if token else None
    if not s:
        raise HTTPException(status_code=401, detail="Sessão inválida ou expirada.")
    return s


# ===== Fluxo de autenticação por QR-code ==================================
@app.post("/api/auth/session", response_model=SessionResponse)
async def create_session():
    """A TV chama isto quando não tem token; recebe o QR para exibir."""
    s = await store.create_qr()
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


@app.post("/api/auth/login")
async def do_login(body: LoginRequest):
    """O celular envia as credenciais do FullTrack e libera a TV."""
    qr = await store.get_qr(body.session_uuid)
    if not qr or qr.status == "expired":
        raise HTTPException(status_code=410, detail="QR-code expirado. Gere um novo na TV.")
    if qr.status != "pending":
        raise HTTPException(status_code=409, detail="Esta sessão já foi autorizada.")
    try:
        auth = await fulltrack.login(body.login, body.password)
    except fulltrack.AuthError as e:
        raise HTTPException(status_code=401, detail=str(e))
    app_token = await store.authorize_qr(body.session_uuid, auth)
    if not app_token:
        raise HTTPException(status_code=410, detail="QR-code expirado. Gere um novo na TV.")
    return {"ok": True}


@app.get("/api/auth/status/{uuid}", response_model=StatusResponse)
async def auth_status(uuid: str):
    """A TV faz polling aqui até virar 'authorized'."""
    s = await store.get_qr(uuid)
    if not s:
        return StatusResponse(status="expired")
    return StatusResponse(status=s.status,
                          access_token=s.app_token if s.status == "authorized" else None)


@app.post("/api/auth/logout")
async def logout(session: AppSession = Depends(current_session)):
    await store.drop_app(session.app_token)
    return {"ok": True}


# ===== Dados =============================================================
@app.get("/api/fleet", response_model=list[Vehicle])
async def fleet(session: AppSession = Depends(current_session)):
    """A sessão da TV não cai por instabilidade do FullTrack: o cliente já refaz
    o login sozinho. Só credencial que deixou de valer (senha trocada) devolve
    401 e manda a TV para um QR novo; o resto é 503 e a TV segue tentando."""
    try:
        return await fulltrack.get_fleet(session.auth)
    except fulltrack.AuthError as e:
        await store.drop_app(session.app_token)
        raise HTTPException(status_code=401, detail=str(e))
    except (fulltrack.SessionExpired, httpx.HTTPError):
        raise HTTPException(status_code=503, detail="FullTrack indisponível no momento.")


@app.get("/api/notifications/total")
async def notifications_total(session: AppSession = Depends(current_session)):
    try:
        return {"total_unread": await fulltrack.get_notifications_total(session.auth)}
    except httpx.HTTPError:
        return {"total_unread": 0}


# ===== Configuração do mosaico salvo ====================================
@app.get("/api/config", response_model=MosaicConfig)
async def get_config():
    return storage.load_config()


@app.post("/api/config", response_model=MosaicConfig)
async def set_config(cfg: MosaicConfig, session: AppSession = Depends(current_session)):
    return storage.save_config(cfg)


# ----- Configurar pelo celular via QR (handoff) --------------------------
@app.post("/api/config/handoff")
async def config_handoff(session: AppSession = Depends(current_session)):
    """A TV pede um QR para configurar no celular (token curto e de uso único)."""
    h = await store.create_handoff(session.auth)
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
async def config_handoff_redeem(uuid: str):
    """O celular troca o UUID do QR por um token curto para configurar."""
    token = await store.redeem_handoff(uuid)
    if not token:
        raise HTTPException(status_code=410, detail="QR de configuração expirado ou já usado.")
    return {"access_token": token}


# ===== SGI (tela secundária) =============================================
@app.get("/api/sgi/status")
async def sgi_status():
    """Estado do SGI para a tela. Sem autenticação: não devolve dado nenhum do
    SGI, só se está ligado — e a tela de configuração precisa disso para saber
    qual formulário mostrar antes de qualquer token."""
    st = sgi.status()
    st["base_url"] = settings.sgi_base_url
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
                       session: AppSession = Depends(current_session)):
    """Resumo de ordens de serviço de HOJE. Painel é real time: sem seletor de data.

    Equipe e tipo(s) de OS vêm da configuração salva — nenhum filtro fixo no
    código. `city` e `category_id`, opcionais, descem de nível na tela
    secundária: cidade -> categoria -> status (`scheduled`/`inProgress`/
    `expired`), cada um filtrando o resumo mais fundo que o de cima.
    """
    if not sgi.is_enabled():
        raise HTTPException(status_code=409, detail="SGI não habilitado.")
    cfg = storage.load_config()
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


app.mount("/assets", NoCacheStatic(directory=FRONTEND / "assets"), name="assets")
