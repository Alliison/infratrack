import hashlib
import io
from pathlib import Path

import httpx
import qrcode
from fastapi import Depends, FastAPI, HTTPException, Header
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import fulltrack, storage
from .config import settings
from .models import (LoginRequest, MosaicConfig, SessionResponse, StatusResponse,
                     Vehicle)
from .sessions import AppSession, store

app = FastAPI(title="TrackInfra", version="1.0.0")

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"


def _asset_version() -> str:
    """Impressao digital do front servido.

    E' o que permite a TV se atualizar sozinha: ela guarda esta string no load e
    recarrega quando ela muda. Derivada do CONTEUDO dos arquivos, nunca da hora
    de boot — com timestamp, todo restart do backend recarregaria os paineis a
    toa, e um deploy que nao mexeu no front nao deve mexer na tela de ninguem.
    """
    h = hashlib.sha256()
    for p in sorted(FRONTEND.rglob("*")):
        if p.is_file():
            h.update(p.relative_to(FRONTEND).as_posix().encode())
            h.update(p.read_bytes())
    return h.hexdigest()[:12]


ASSET_VERSION = _asset_version()


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


# ===== Front (páginas) ===================================================
@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/api/version")
async def version():
    """Versao do front. A TV compara com a que carregou e se recarrega sozinha
    quando muda — e' o unico jeito de publicar front novo num painel de parede,
    que nao tem quem aperte F5."""
    return JSONResponse({"version": ASSET_VERSION},
                        headers={"Cache-Control": "no-store"})


@app.get("/")
async def index():
    # no-cache tambem aqui: de nada adianta a TV recarregar se o proprio HTML
    # vier do cache apontando para os assets antigos.
    return FileResponse(FRONTEND / "index.html",
                        headers={"Cache-Control": "no-cache"})


@app.get("/login")
async def login_page():
    return FileResponse(FRONTEND / "login.html")


@app.get("/config")
async def config_page():
    return FileResponse(FRONTEND / "config.html")


app.mount("/assets", NoCacheStatic(directory=FRONTEND / "assets"), name="assets")
