import io
from pathlib import Path

import qrcode
from fastapi import Depends, FastAPI, HTTPException, Header
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from . import fulltrack, storage
from .config import settings
from .models import (LoginRequest, MosaicConfig, SessionResponse, StatusResponse,
                     Vehicle)
from .sessions import AppSession, store

app = FastAPI(title="TrackInfra", version="1.0.0")

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"


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
    try:
        return await fulltrack.get_fleet(session.auth)
    except fulltrack.SessionExpired:
        await store.drop_app(session.app_token)
        raise HTTPException(status_code=401, detail="Sessão do FullTrack expirou.")


@app.get("/api/notifications/total")
async def notifications_total(session: AppSession = Depends(current_session)):
    return {"total_unread": await fulltrack.get_notifications_total(session.auth)}


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


@app.get("/")
async def index():
    return FileResponse(FRONTEND / "index.html")


@app.get("/login")
async def login_page():
    return FileResponse(FRONTEND / "login.html")


@app.get("/config")
async def config_page():
    return FileResponse(FRONTEND / "config.html")


app.mount("/assets", StaticFiles(directory=FRONTEND / "assets"), name="assets")
