from typing import Literal, Optional

from pydantic import BaseModel


class LoginRequest(BaseModel):
    session_uuid: str
    login: str
    password: str


class SessionResponse(BaseModel):
    session_uuid: str
    login_url: str
    qr_url: str
    expires_in: int


class StatusResponse(BaseModel):
    status: str  # "pending" | "authorized" | "expired"
    access_token: Optional[str] = None


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

    # Escolhidos no modal de confirmação do celular. "auto" mantém o
    # comportamento antigo: grade deduzida da quantidade de carros e ligados
    # na frente. É para onde o botão "Seguir automático" devolve tudo.
    grid: Literal["auto", "2x2", "2x3"] = "auto"
    sequencia_manual: bool = False  # respeitar a ordem de selected_ids
