"""Contas, dispositivos e tokens — tudo em memória.

Modelo:
  - Account: uma conta do FullTrack (chave = usuário do login, normalizado).
    Guarda a ÚNICA sessão do FullTrack daquela conta; todas as TVs e celulares
    dela compartilham o mesmo `FulltrackAuth` (um re-login serve a todos).
  - AppSession (grant): um token opaco nosso. `kind` diz quem segura:
      "tv"         uma TV — aponta para um dispositivo (`device_id`), que tem
                   nome e configuração próprios (ver devices.py);
      "controller" um celular que gerencia a conta: vincula TVs novas sem pedir
                   a senha de novo, configura e desconecta cada uma.
  - QRSession: efêmera, criada pela TV; vira "authorized" quando um celular
    loga (senha) ou vincula (controle já logado).
  - HandoffSession: QR "Configurar" da TV — troca única por um token de
    controle curto, já apontado para aquela TV.

Revogação: token derrubado de propósito (desconectar TV, desconectar tudo,
senha trocada) entra em `_revoked` por um tempo, e a API responde 401 com
`X-Session-Revoked: 1` — a TV vai direto para o QR em vez de tolerar o 401
como blip.

Em produção com múltiplas instâncias, trocar por Redis. Para 1 processo,
memória é suficiente (e mantém as credenciais fora de disco — exceto com
TRACKINFRA_PERSIST_SESSIONS, ver persist.py).
"""
import asyncio
import secrets
import time
from dataclasses import dataclass, field
from typing import Literal, Optional

from .config import settings


def account_key(login_user: str) -> str:
    return (login_user or "").strip().lower()


@dataclass
class FulltrackAuth:
    """Credenciais vivas de uma sessão FullTrack (nunca vão para a TV)."""
    cookies: dict[str, str]
    token: dict          # { access_token, refresh_token, expires_in, ... }
    token_obtained_at: float = field(default_factory=time.time)
    # Guardadas só em RAM, para o re-login automático: o cookie de sessão do
    # FullTrack cai sozinho e sem isto a TV voltaria para o QR.
    login_user: Optional[str] = None
    password: Optional[str] = None
    relogin_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)


@dataclass
class Account:
    id: str                      # account_key(login_user)
    auth: FulltrackAuth
    created_at: float = field(default_factory=time.time)

    @property
    def login_user(self) -> str:
        return self.auth.login_user or self.id


Kind = Literal["tv", "controller"]


@dataclass
class AppSession:
    app_token: str
    account: Account
    kind: Kind = "tv"
    device_id: Optional[str] = None   # tv: o dispositivo; controller: a TV do handoff, se houver
    # id público (listar/revogar sem expor o token)
    id: str = field(default_factory=lambda: secrets.token_urlsafe(8))
    label: str = ""                   # controller: descrição do celular (user-agent)
    created_at: float = field(default_factory=time.time)
    last_used_at: float = field(default_factory=time.time)
    # Expiração por inatividade. 0 = sem expirar (TV, e celular com
    # controller_token_ttl=0). Conta a partir do último uso, não da criação:
    # um painel aberto no celular não cai no meio do uso.
    idle_ttl: float = 0

    @property
    def auth(self) -> FulltrackAuth:
        return self.account.auth

    def expired(self, now: float) -> bool:
        return self.idle_ttl > 0 and now - self.last_used_at > self.idle_ttl


@dataclass
class QRSession:
    uuid: str
    created_at: float = field(default_factory=time.time)
    status: str = "pending"           # pending | authorized | expired
    app_token: Optional[str] = None
    device_id: Optional[str] = None   # pista da TV (quem ela era) / resultado


@dataclass
class HandoffSession:
    """QR para configurar pelo celular: troca única por um token curto."""
    uuid: str
    account_id: str
    device_id: Optional[str]
    created_at: float = field(default_factory=time.time)
    used: bool = False


REVOKED_KEEP_S = 24 * 3600


class SessionStore:
    def __init__(self) -> None:
        self._qr: dict[str, QRSession] = {}
        self._app: dict[str, AppSession] = {}
        self._accounts: dict[str, Account] = {}
        self._handoff: dict[str, HandoffSession] = {}
        self._revoked: dict[str, float] = {}
        self._lock = asyncio.Lock()

    # ---- contas ------------------------------------------------------------
    async def upsert_account(self, auth: FulltrackAuth) -> Account:
        """Login com senha: entra na conta existente do mesmo usuário (renovando
        a sessão do FullTrack dela) ou cria uma. É isso que faz a 2ª TV logada
        com senha cair na mesma conta da 1ª, e não numa conta paralela."""
        async with self._lock:
            key = account_key(auth.login_user or "")
            acc = self._accounts.get(key)
            if acc:
                acc.auth.cookies = auth.cookies
                acc.auth.token = auth.token
                acc.auth.token_obtained_at = auth.token_obtained_at
                acc.auth.password = auth.password
            else:
                acc = Account(id=key, auth=auth)
                self._accounts[key] = acc
            return acc

    def account(self, account_id: str) -> Optional[Account]:
        return self._accounts.get(account_id)

    def grants_of(self, account_id: str) -> list[AppSession]:
        return [s for s in self._app.values() if s.account.id == account_id]

    async def drop_account(self, account_id: str) -> int:
        """Desconectar tudo: todos os tokens da conta caem e a sessão do
        FullTrack sai da memória. Os dispositivos (nome/config) ficam no
        registro — relogar devolve cada TV como estava."""
        async with self._lock:
            n = 0
            for tok in [t for t, s in self._app.items() if s.account.id == account_id]:
                self._revoke(tok)
                n += 1
            acc = self._accounts.pop(account_id, None)
            if acc:
                acc.auth.password = None
                acc.auth.cookies = {}
            return n

    # ---- QR sessions -------------------------------------------------------
    async def create_qr(self, device_hint: Optional[str] = None) -> QRSession:
        async with self._lock:
            self._gc()
            uuid = secrets.token_urlsafe(18)
            s = QRSession(uuid=uuid, device_id=device_hint)
            self._qr[uuid] = s
            return s

    async def get_qr(self, uuid: str) -> Optional[QRSession]:
        async with self._lock:
            s = self._qr.get(uuid)
            if s and s.status == "pending" and self._qr_expired(s):
                s.status = "expired"
            return s

    async def authorize_qr(self, uuid: str, account: Account, device_id: str) -> Optional[str]:
        """Cria o token da TV para `device_id` e libera o QR. Um dispositivo tem
        no máximo um token de TV: o anterior (se houver) é revogado — a TV
        antiga que ainda o tivesse cai no QR em vez de virar uma TV-fantasma."""
        async with self._lock:
            s = self._qr.get(uuid)
            if not s or self._qr_expired(s) or s.status != "pending":
                return None
            for tok in [t for t, a in self._app.items()
                        if a.kind == "tv" and a.device_id == device_id]:
                self._revoke(tok)
            app_token = secrets.token_urlsafe(32)
            self._app[app_token] = AppSession(app_token=app_token, account=account,
                                              kind="tv", device_id=device_id)
            s.status = "authorized"
            s.app_token = app_token
            s.device_id = device_id
            return app_token

    # ---- tokens ------------------------------------------------------------
    async def new_controller(self, account: Account, label: str = "",
                             idle_ttl: Optional[float] = None,
                             device_id: Optional[str] = None) -> AppSession:
        async with self._lock:
            tok = secrets.token_urlsafe(32)
            s = AppSession(app_token=tok, account=account, kind="controller", label=label,
                           device_id=device_id,
                           idle_ttl=settings.controller_token_ttl if idle_ttl is None else idle_ttl)
            self._app[tok] = s
            return s

    async def get_app(self, app_token: str) -> Optional[AppSession]:
        async with self._lock:
            self._gc()
            s = self._app.get(app_token)
            if not s:
                return None
            s.last_used_at = time.time()
            return s

    def is_revoked(self, app_token: str) -> bool:
        return app_token in self._revoked

    async def drop_app(self, app_token: str) -> None:
        async with self._lock:
            self._revoke(app_token)

    async def drop_by_id(self, account_id: str, grant_id: str) -> bool:
        async with self._lock:
            for tok, s in list(self._app.items()):
                if s.account.id == account_id and s.id == grant_id:
                    self._revoke(tok)
                    return True
            return False

    async def drop_device(self, account_id: str, device_id: str) -> int:
        """Desconecta UMA TV: só o(s) token(s) dela caem. A conta e as outras
        TVs seguem no ar."""
        async with self._lock:
            toks = [t for t, s in self._app.items()
                    if s.account.id == account_id and s.kind == "tv" and s.device_id == device_id]
            for t in toks:
                self._revoke(t)
            return len(toks)

    def device_connected(self, device_id: str) -> bool:
        return any(s.kind == "tv" and s.device_id == device_id for s in self._app.values())

    # ---- Handoff (configurar pelo celular via QR) --------------------------
    async def create_handoff(self, session: AppSession) -> HandoffSession:
        async with self._lock:
            self._gc()
            uuid = secrets.token_urlsafe(18)
            h = HandoffSession(uuid=uuid, account_id=session.account.id,
                               device_id=session.device_id)
            self._handoff[uuid] = h
            return h

    async def get_handoff(self, uuid: str) -> Optional[HandoffSession]:
        async with self._lock:
            return self._handoff.get(uuid)

    async def redeem_handoff(self, uuid: str, label: str = "") -> Optional[AppSession]:
        """Troca única: devolve um token de controle curto (15 min parado) para
        o celular, já apontado para a TV que mostrou o QR."""
        async with self._lock:
            h = self._handoff.get(uuid)
            if not h or h.used or time.time() - h.created_at > settings.qr_session_ttl:
                return None
            acc = self._accounts.get(h.account_id)
            if not acc:
                return None
            h.used = True
            tok = secrets.token_urlsafe(32)
            s = AppSession(app_token=tok, account=acc, kind="controller",
                           device_id=h.device_id, label=label, idle_ttl=900)
            self._app[tok] = s
            return s

    # ---- helpers -----------------------------------------------------------
    def _revoke(self, tok: str) -> None:
        if self._app.pop(tok, None) is not None:
            self._revoked[tok] = time.time()

    def _qr_expired(self, s: QRSession) -> bool:
        return time.time() - s.created_at > settings.qr_session_ttl

    def _gc(self) -> None:
        now = time.time()
        for uuid in [u for u, s in self._qr.items()
                     if now - s.created_at > settings.qr_session_ttl and s.status != "authorized"]:
            self._qr.pop(uuid, None)
        # QR já autorizado: a TV leva o token no próximo polling; depois disso
        # ele só ocupa memória.
        for uuid in [u for u, s in self._qr.items()
                     if s.status == "authorized" and now - s.created_at > 2 * settings.qr_session_ttl]:
            self._qr.pop(uuid, None)
        for tok in [t for t, s in self._app.items() if s.expired(now)]:
            self._app.pop(tok, None)
        for u in [u for u, h in self._handoff.items()
                  if now - h.created_at > settings.qr_session_ttl]:
            self._handoff.pop(u, None)
        for t in [t for t, at in self._revoked.items() if now - at > REVOKED_KEEP_S]:
            self._revoked.pop(t, None)


store = SessionStore()
