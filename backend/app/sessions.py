"""Armazenamento em memória de sessões.

Dois tipos:
  - QRSession: efêmera, criada pela TV; vira "authorized" quando o celular loga.
  - AppSession: sessão longa da TV, guarda os cookies + token do FullTrack.

Em produção com múltiplas instâncias, trocar por Redis. Para uma TV/1 processo,
memória é suficiente (e mantém as credenciais fora de disco).
"""
import asyncio
import secrets
import time
from dataclasses import dataclass, field
from typing import Optional

from .config import settings


@dataclass
class FulltrackAuth:
    """Credenciais vivas de uma sessão FullTrack (nunca vão para a TV)."""
    cookies: dict[str, str]
    token: dict          # { access_token, refresh_token, expires_in, ... }
    token_obtained_at: float = field(default_factory=time.time)


@dataclass
class AppSession:
    app_token: str
    auth: FulltrackAuth
    created_at: float = field(default_factory=time.time)
    last_used_at: float = field(default_factory=time.time)
    ttl: float = 0                    # 0 = usa settings.app_token_ttl


@dataclass
class QRSession:
    uuid: str
    created_at: float = field(default_factory=time.time)
    status: str = "pending"           # pending | authorized | expired
    app_token: Optional[str] = None


@dataclass
class HandoffSession:
    """QR para configurar pelo celular: troca única por um token curto."""
    uuid: str
    auth: FulltrackAuth
    created_at: float = field(default_factory=time.time)
    used: bool = False


class SessionStore:
    def __init__(self) -> None:
        self._qr: dict[str, QRSession] = {}
        self._app: dict[str, AppSession] = {}
        self._handoff: dict[str, HandoffSession] = {}
        self._lock = asyncio.Lock()

    # ---- QR sessions -----------------------------------------------------
    async def create_qr(self) -> QRSession:
        async with self._lock:
            self._gc()
            uuid = secrets.token_urlsafe(18)
            s = QRSession(uuid=uuid)
            self._qr[uuid] = s
            return s

    async def get_qr(self, uuid: str) -> Optional[QRSession]:
        async with self._lock:
            s = self._qr.get(uuid)
            if s and self._qr_expired(s):
                s.status = "expired"
            return s

    async def authorize_qr(self, uuid: str, auth: FulltrackAuth) -> Optional[str]:
        """Vincula um AppSession recém-criado a uma QRSession pendente."""
        async with self._lock:
            s = self._qr.get(uuid)
            if not s or self._qr_expired(s) or s.status != "pending":
                return None
            app_token = secrets.token_urlsafe(32)
            self._app[app_token] = AppSession(app_token=app_token, auth=auth)
            s.status = "authorized"
            s.app_token = app_token
            return app_token

    # ---- App sessions ----------------------------------------------------
    async def get_app(self, app_token: str) -> Optional[AppSession]:
        async with self._lock:
            self._gc()
            s = self._app.get(app_token)
            if not s:
                return None
            ttl = s.ttl or settings.app_token_ttl
            if time.time() - s.created_at > ttl:
                self._app.pop(app_token, None)
                return None
            s.last_used_at = time.time()
            return s

    async def drop_app(self, app_token: str) -> None:
        async with self._lock:
            self._app.pop(app_token, None)

    # ---- Handoff (configurar pelo celular via QR) ------------------------
    async def create_handoff(self, auth: FulltrackAuth) -> HandoffSession:
        async with self._lock:
            self._gc()
            uuid = secrets.token_urlsafe(18)
            h = HandoffSession(uuid=uuid, auth=auth)
            self._handoff[uuid] = h
            return h

    async def redeem_handoff(self, uuid: str) -> Optional[str]:
        """Troca única: devolve um token curto (15 min) para o celular."""
        async with self._lock:
            h = self._handoff.get(uuid)
            if not h or h.used or time.time() - h.created_at > settings.qr_session_ttl:
                return None
            h.used = True
            app_token = secrets.token_urlsafe(32)
            self._app[app_token] = AppSession(app_token=app_token, auth=h.auth, ttl=900)
            return app_token

    # ---- helpers ---------------------------------------------------------
    def _qr_expired(self, s: QRSession) -> bool:
        return time.time() - s.created_at > settings.qr_session_ttl

    def _gc(self) -> None:
        now = time.time()
        for uuid in [u for u, s in self._qr.items()
                     if now - s.created_at > settings.qr_session_ttl and s.status != "authorized"]:
            self._qr.pop(uuid, None)
        for tok in [t for t, s in self._app.items()
                    if now - s.created_at > (s.ttl or settings.app_token_ttl)]:
            self._app.pop(tok, None)
        for u in [u for u, h in self._handoff.items()
                  if now - h.created_at > settings.qr_session_ttl]:
            self._handoff.pop(u, None)


store = SessionStore()
