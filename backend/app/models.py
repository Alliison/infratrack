from typing import Literal, Optional

from pydantic import BaseModel


class LoginRequest(BaseModel):
    session_uuid: str
    login: str
    password: str


class SessionRequest(BaseModel):
    """Corpo opcional do POST /api/auth/session: quem a TV era (se lembra)."""
    device_id: Optional[str] = None


class PairRequest(BaseModel):
    """Celular já logado (token de controle) liberando uma TV nova, sem senha."""
    session_uuid: str
    name: Optional[str] = None


class RenameRequest(BaseModel):
    name: str


class CommandRequest(BaseModel):
    cmd: Literal["identify", "reload"]


class SessionResponse(BaseModel):
    session_uuid: str
    login_url: str
    qr_url: str
    expires_in: int


class StatusResponse(BaseModel):
    status: str  # "pending" | "authorized" | "expired"
    access_token: Optional[str] = None
    device_id: Optional[str] = None


class Vehicle(BaseModel):
    id: str
    placa: Optional[str] = None
    modelo: Optional[str] = None
    cor: Optional[str] = None
    lat: Optional[float] = None
    lng: Optional[float] = None
    velocidade: float = 0
    ignicao: bool = False
    data_gps: Optional[str] = None
    motorista: Optional[str] = None
    ref_proxima: Optional[str] = None


class MosaicConfig(BaseModel):
    selected_ids: list[str] = []   # vazio = todos os veículos; a ORDEM importa
                                   # quando sequencia_manual estiver ligado
    only_ligados: bool = True      # exibir só carros com ignição ligada
    zoom: int = 15
    refresh_seconds: int = 6
    rotativo: bool = False          # alternar páginas automaticamente
    rotate_seconds: int = 15        # segundos por página (configurável)
    page_size: int = 9              # telas por página no modo rotativo
    # Inclui a tela do SGI como última página do rodízio (só vale com `rotativo`).
    sgi_in_rotation: bool = False
    # Trava a exibição numa tela só, sem desligar os rodízios de cada uma:
    # "fulltrack" = só o mosaico (grids seguem girando), "sgi" = só o SGI
    # (cidades/técnicos seguem girando), "none" = alterna como configurado.
    screen_lock: Literal["none", "fulltrack", "sgi"] = "none"
    # Tela do SGI com a gaveta da OS aberta em TODOS os técnicos ao mesmo tempo
    # (em vez de um por vez, girando). O front encolhe fontes e espaços para
    # caber todo mundo na altura da tela.
    techs_expanded: bool = False

    # Escolhidos no modal de confirmação do celular. "auto" mantém o
    # comportamento antigo: grade deduzida da quantidade de carros e ligados
    # na frente. É para onde o botão "Seguir automático" devolve tudo.
    grid: Literal["auto", "2x2", "2x3"] = "auto"
    sequencia_manual: bool = False  # respeitar a ordem de selected_ids

    # --- Tela secundária (SGI) ---
    # ⚠️ Credencial do SGI NÃO entra aqui: este modelo é gravado em disco
    # (mosaic_config.json). Usuário e senha ficam só em RAM, em sgi.py.
    # `sgi_enabled` guarda apenas a intenção — depois de um restart ele continua
    # True mas o SGI está desligado, e a tela usa isso para pedir o login de novo
    # em vez de fingir que está tudo certo.
    sgi_enabled: bool = False
    sgi_team: Optional[str] = None   # equipe escolhida; None = sem filtro
    # Tipos de OS escolhidos na tela. None = ainda não mexeu nisso pelo celular
    # (usa o padrão do ambiente, `settings.sgi_types`); lista vazia = "todos os
    # tipos" escolhido explicitamente (sem filtro, igual à semântica do time).
    sgi_types: Optional[list[str]] = None


class SgiEnableRequest(BaseModel):
    """Credencial do SGI vinda da tela de configuração. Não é persistida."""
    username: str
    password: str
    totp_secret: Optional[str] = None   # chave base32 do 2FA (opcional)
