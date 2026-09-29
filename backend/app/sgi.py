"""Cliente do SGI — origem dos dados da tela secundária.

Espelha o fluxo que o `impacto-notification-service` já roda em produção:

    POST api/core/v1/auth/login   OAuth2 password grant, form-urlencoded, scope=admin
    GET  api/core/v1/auth/me      2xx = token vivo

Três diferenças deliberadas em relação a esse serviço:

1. **A credencial não vem do `.env`.** Quem habilita é o usuário, pela tela de
   configuração no celular, e usuário/senha ficam **só em RAM** — mesma política
   da senha do FullTrack (ver `sessions.FulltrackAuth`). Restart do backend
   derruba o SGI junto com a sessão da TV, e é preciso habilitar de novo. O
   preço é conhecido; a alternativa seria gravar senha em disco.

2. **A renovação é proativa** quando o token traz `exp`: o health check renova
   na última hora de vida em vez de esperar o 401. Sem `exp` no JWT, cai no modo
   reativo (valida em `/auth/me`, refaz o login quando falhar), que é o que o
   notification-service faz.

3. **Estado global, não por sessão.** É um único painel de parede, de uma única
   empresa: a TV e o celular compartilham o mesmo SGI. Sessão da TV e handoff do
   celular já compartilham o mesmo objeto de auth do FullTrack pelo mesmo motivo.

Detalhe herdado de lá e mantido: **o `/auth/me` dá falso positivo.** Quando o
endpoint de dados devolve 401 com um token que o `/me` ainda aceitava, a única
saída é forçar o login — daí o `force=True` em `valid_token()`.
"""
import asyncio
import base64
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional
from zoneinfo import ZoneInfo

import httpx

from .config import settings

logger = logging.getLogger(__name__)

# A VM roda em UTC, não no fuso da empresa. `date.today()` usaria o relógio do
# sistema (UTC) — certo de dia, mas ERRADO à noite: das 21h às 23h59 em
# Brasília já é meia-noite em UTC, e o painel passaria a filtrar as OSs de
# AMANHÃ. O "hoje" do relatório tem que ser sempre o dia civil em Brasília,
# não o do servidor.
BUSINESS_TZ = ZoneInfo("America/Sao_Paulo")


def today() -> date:
    return datetime.now(BUSINESS_TZ).date()

# Caminhos conhecidos do SGI. Não são chute: saem da spec OpenAPI que o próprio
# SGI publica em /api/core/openapi.json, e `discover()` confirma que continuam
# existindo antes de qualquer consulta. Ficam aqui só como ponto de partida para
# a descoberta (e como fallback se a spec estiver fora do ar).
OPENAPI_PATH = "api/core/openapi.json"
LOGIN_PATH = "api/core/v1/auth/login"
ME_PATH = "api/core/v1/auth/me"
SUMMARY_PATH = "api/core/v1/reports/service-orders/summary"
TECHNICIANS_PATH = "api/core/v1/technicians/"
SERVICE_ORDER_PATH = "api/core/v1/service-orders/"
TEAMS_PATH = "api/core/v1/teams/"
TEAMS_ME_PATH = "api/core/v1/teams/me"


@dataclass
class SgiState:
    """Credencial viva do SGI. Nunca serializada, nunca gravada em disco."""
    username: str
    password: str                      # só em RAM, para o re-login automático
    token: str = ""
    expires_at: Optional[float] = None  # do claim `exp`; None = JWT sem validade legível
    obtained_at: float = field(default_factory=time.time)
    last_check_at: Optional[float] = None
    last_error: Optional[str] = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)


_state: Optional[SgiState] = None


def _base() -> str:
    return settings.sgi_base_url.rstrip("/")


def _decode_exp(token: str) -> Optional[float]:
    """Lê o claim `exp` do JWT sem validar assinatura (só o backend do SGI valida).

    Serve exclusivamente para decidir *quando* renovar. Token opaco ou sem `exp`
    devolve None, e aí a renovação passa a ser reativa.
    """
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        exp = claims.get("exp")
        return float(exp) if exp else None
    except Exception:  # noqa: BLE001 — token opaco é caso previsto, não erro
        return None


async def _login(username: str, password: str) -> tuple[str, Optional[float]]:
    """OAuth2 password grant. Devolve (token, expires_at) ou levanta RuntimeError."""
    data = {
        "grant_type": "password",
        "username": username,
        "password": password,
        "scope": "admin",
        "client_id": "",
        "client_secret": "",
    }
    headers = {
        "accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(f"{_base()}/{LOGIN_PATH}", data=data, headers=headers)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"SGI inacessível: {exc}") from exc

    if r.status_code in (400, 401, 403):
        raise RuntimeError("Usuário ou senha do SGI incorretos.")
    if r.is_error:
        raise RuntimeError(f"SGI recusou o login (HTTP {r.status_code}).")

    token = (r.json().get("access_token") or "").strip()
    if not token:
        raise RuntimeError("SGI não devolveu 'access_token'.")
    return token, _decode_exp(token)


async def _me_ok(token: str) -> bool:
    """True se o `/auth/me` aceitar o token. Falha de rede conta como inválido."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"{_base()}/{ME_PATH}",
                headers={"accept": "application/json", "Authorization": f"Bearer {token}"},
            )
        return r.is_success
    except Exception as exc:  # noqa: BLE001
        logger.debug("SGI: falha ao validar token em /auth/me: %s", exc)
        return False


async def enable(username: str, password: str) -> dict:
    """Habilita o SGI: faz login e guarda a credencial em RAM."""
    global _state
    token, exp = await _login(username, password)
    _state = SgiState(username=username, password=password, token=token, expires_at=exp,
                      last_check_at=time.time())
    logger.info("SGI habilitado para %s (exp %s)", username,
                "ausente" if exp is None else f"em {(exp - time.time()) / 3600:.1f}h")
    # Confere o contrato na spec assim que há token: melhor descobrir agora que na TV.
    await discover()
    return status()


async def disable() -> dict:
    global _state
    _state = None
    logger.info("SGI desabilitado; credencial descartada da memória.")
    return status()


def status() -> dict:
    """Estado para a tela. Nunca inclui senha nem o token."""
    s = _state
    if not s:
        return {"enabled": False}
    return {
        "enabled": True,
        "username": s.username,
        # None quando o JWT não traz `exp` — a tela avisa que o modo é reativo.
        "expires_in": None if s.expires_at is None else max(0, int(s.expires_at - time.time())),
        "proactive_refresh": s.expires_at is not None,
        "last_check_in": None if s.last_check_at is None else int(time.time() - s.last_check_at),
        "last_error": s.last_error,
    }


def is_enabled() -> bool:
    return _state is not None


async def valid_token(force: bool = False) -> str:
    """Devolve um token utilizável, renovando quando necessário.

    `force=True` pula a validação e vai direto ao login: é o caminho para o
    falso positivo do `/auth/me` (endpoint de dados devolveu 401 mas o /me
    aceitava o token).
    """
    s = _state
    if not s:
        raise RuntimeError("SGI não habilitado.")

    async with s.lock:
        precisa = force
        if not precisa and s.expires_at is not None:
            # Proativo: renova antes de morrer, sem esperar o primeiro 401.
            precisa = (s.expires_at - time.time()) <= settings.sgi_refresh_margin
        if not precisa:
            precisa = not await _me_ok(s.token)

        if precisa:
            token, exp = await _login(s.username, s.password)
            s.token, s.expires_at, s.obtained_at = token, exp, time.time()
            logger.info("SGI: token renovado.")
        s.last_check_at = time.time()
        s.last_error = None
        return s.token


async def _get(path: str, params: dict | None = None) -> httpx.Response:
    """GET autenticado, com uma segunda tentativa em 401 (falso positivo do /me)."""
    token = await valid_token()
    async with httpx.AsyncClient(timeout=25) as client:
        url = f"{_base()}/{path.lstrip('/')}"
        headers = {"accept": "application/json", "Authorization": f"Bearer {token}"}
        r = await client.get(url, params=params, headers=headers)
        if r.status_code == 401:
            token = await valid_token(force=True)
            headers["Authorization"] = f"Bearer {token}"
            r = await client.get(url, params=params, headers=headers)
    return r


async def fetch_summary(
    day: Optional[date] = None,
    types: Optional[list[str]] = None,
    team_id: Optional[str] = None,
    date_field: Optional[str] = None,
    cities: Optional[list[str]] = None,
    category_ids: Optional[list[str]] = None,
) -> dict:
    """Resumo de ordens de serviço do dia. A data é sempre hoje: o painel é real time.

    Nada aqui é fixo no código: `types`, `team_id` e `date_field` vêm da
    configuração salva, e o nome do parâmetro de equipe (`teamId`) e o enum de
    `dateField` saem da spec OpenAPI do SGI, não de suposição.

    Na spec, `type` é **array** — httpx serializa a lista como `type=a&type=b`,
    que é o que o FastAPI do SGI espera. Mandar string funcionaria por acidente
    com um valor só e quebraria no segundo.

    `cities` e `category_ids` são o que a tela secundária usa pra descer de
    nível (cidade -> categoria -> status): o relatório não cruza essas
    dimensões numa resposta só (`ordersByCity`/`ordersByCategory` são
    agregados independentes, e nenhum dos dois traz `scheduled`/`inProgress`/
    `expired` por dentro), então cada nível pede o resumo de novo, filtrado
    mais fundo, e lê os campos que precisa dessa resposta.
    """
    d = (day or today()).isoformat()
    params: dict = {
        "startDate": d,
        "endDate": d,
        "dateField": date_field or settings.sgi_date_field,
        "type": types if types is not None else list(settings.sgi_types),
    }
    if team_id:
        params["teamId"] = team_id
    if cities:
        params["cities"] = cities
    if category_ids:
        params["categoryId"] = category_ids
    r = await _get(SUMMARY_PATH, params)
    r.raise_for_status()
    body = r.json()
    # Logado de propósito enquanto a tela secundária está sendo desenhada: é como
    # se descobre a estrutura real sem ninguém manusear credencial no terminal.
    logger.info("SGI summary %s params=%s -> %s bytes | %s", d,
                {k: v for k, v in params.items() if k != "startDate"}, len(r.content),
                json.dumps(body, ensure_ascii=False)[:1500])
    return body


# ---------------------------------------------------------------------------
# Descoberta pela spec: o SGI publica OpenAPI em /api/core/openapi.json (mesma
# convenção do impacto-notification-service, que expõe /api/core/openapi.json).
# Antes disso aqui havia uma lista de endpoints CHUTADOS — que falhava
# silenciosamente e dava a impressão de que a integração estava quebrada.
# ---------------------------------------------------------------------------
_spec: Optional[dict] = None


_started_cache: dict[str, tuple] = {}   # id da OS -> (startedAt, sla) (só guarda quando já iniciou)
_started_logged: set[str] = set()     # evita repetir o mesmo log a cada ciclo de 6s


def _log_once(key: str, msg: str, *args) -> None:
    if key not in _started_logged:
        _started_logged.add(key)
        logger.warning(msg, *args)


async def _started_at(os_id: str, os_status: str = "") -> tuple:
    """`startedAt` não vem na ficha do técnico, só no detalhe da OS. Cacheado:
    depois de preenchido o valor não muda, então cada OS é consultada até
    iniciar e nunca mais."""
    if os_id in _started_cache:
        return _started_cache[os_id]
    try:
        r = await _get(f"{SERVICE_ORDER_PATH}{os_id}")
        r.raise_for_status()
        body = r.json()
        v = body.get("startedAt")
        sla = (body.get("category") or {}).get("sla")
    except Exception as exc:  # noqa: BLE001 — detalhe opcional, não derruba a lista
        _log_once(f"err:{os_id}", "SGI startedAt: falha ao consultar a OS %s (status %r): %r",
                  os_id[:8], os_status, exc)
        return None, None
    if v:
        _started_cache[os_id] = (v, sla)
    return v, sla


async def fetch_technicians() -> dict[str, dict]:
    """Ficha em tempo real de cada técnico, por id: {id: {status, isAvailable, ...}}.

    Vem de `/technicians/` (TechnicianSchema), casado com o resumo de ordens
    pelo `id`. O `/realtime/` não serve: só traz id/status/lat/lng.
    """
    out: dict[str, dict] = {}
    page = 1
    while True:
        r = await _get(TECHNICIANS_PATH, {"page": page, "size": 1000})
        r.raise_for_status()
        body = r.json()
        for it in body.get("list") or []:
            out[str(it.get("id"))] = {
                "status": it.get("status") or "",
                "isAvailable": it.get("isAvailable"),
                "onlineAt": it.get("onlineAt"),
                "lastLocationAt": it.get("lastLocationAt"),
                "currentServiceOrder": it.get("currentServiceOrder"),
            }
        if page >= int((body.get("pagination") or {}).get("pages") or 1):
            break
        page += 1

    sem = asyncio.Semaphore(5)

    async def fill(info: dict) -> None:
        os_ = info.get("currentServiceOrder")
        if os_ and os_.get("id"):
            async with sem:
                os_["startedAt"], os_["sla"] = await _started_at(
                    str(os_["id"]), str(os_.get("status") or ""))

    await asyncio.gather(*(fill(i) for i in out.values()))
    return out


async def fetch_spec(refresh: bool = False) -> dict:
    """Baixa (e memoiza) a spec OpenAPI do SGI. Não exige autenticação."""
    global _spec
    if _spec is not None and not refresh:
        return _spec
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f"{_base()}/{OPENAPI_PATH}",
                             headers={"accept": "application/json"})
    r.raise_for_status()
    _spec = r.json()
    return _spec


async def fetch_spec_paths() -> set[str]:
    """Caminhos publicados pela instância. Conjunto vazio se a spec não vier —
    e aí o chamador tenta a rota mesmo, em vez de travar por falta de catálogo."""
    try:
        return set((await fetch_spec()).get("paths", {}))
    except Exception as exc:  # noqa: BLE001
        logger.warning("SGI: spec indisponível ao listar rotas: %s", exc)
        return set()


async def discover() -> dict:
    """Confere na spec quais rotas existem e quais parâmetros o summary aceita.

    Serve para não descobrir na parede que o SGI mudou de contrato: se o
    `teamId` deixar de existir, isto aparece aqui em vez de virar um filtro
    ignorado em silêncio (que é o pior resultado possível — parece certo e está
    errado).
    """
    try:
        spec = await fetch_spec()
    except Exception as exc:  # noqa: BLE001
        logger.warning("SGI: não foi possível ler a spec OpenAPI: %s", exc)
        return {"spec_disponivel": False, "erro": str(exc)}

    paths = spec.get("paths", {})

    def existe(path: str, metodo: str = "get") -> bool:
        return metodo in paths.get(f"/{path.lstrip('/')}", {})

    summary_params: list[str] = []
    op = paths.get(f"/{SUMMARY_PATH}", {}).get("get", {})
    for prm in op.get("parameters", []):
        if prm.get("name"):
            summary_params.append(prm["name"])

    info = {
        "spec_disponivel": True,
        "versao": spec.get("info", {}).get("version"),
        "rotas": len(paths),
        "login": existe(LOGIN_PATH, "post"),
        "me": existe(ME_PATH),
        "summary": existe(SUMMARY_PATH),
        "teams": existe(TEAMS_PATH),
        "teams_me": existe(TEAMS_ME_PATH),
        "summary_aceita_teamId": "teamId" in summary_params,
        "summary_params": summary_params,
    }
    logger.info("SGI discover: %s", json.dumps(
        {k: v for k, v in info.items() if k != "summary_params"}, ensure_ascii=False))
    return info


async def fetch_teams() -> list[dict]:
    """Equipes para o seletor da tela de configuração.

    Tenta primeiro `/teams/me` (as equipes do usuário logado, sem paginação e sem
    parâmetro nenhum) e cai para `/teams/` paginado quando aquele não devolver
    nada — um usuário administrativo pode não pertencer a equipe alguma e ainda
    assim precisar escolher qualquer uma.
    """
    equipes: list[dict] = []

    # A spec diz quais rotas existem NESTA instância: o /teams/me só apareceu na
    # 0.0.478 (dev) e não existe na 0.0.463 (produção). Perguntar antes evita um
    # 404 garantido a cada carga do seletor.
    rotas = (await fetch_spec_paths())
    tem_me = f"/{TEAMS_ME_PATH}" in rotas

    if tem_me:
        r = await _get(TEAMS_ME_PATH)
        if r.is_success:
            body = r.json()
            if isinstance(body, list):
                equipes = body
            logger.info("SGI /teams/me -> %s equipe(s)", len(equipes))
    else:
        logger.info("SGI: /teams/me ausente nesta instância; usando /teams/.")

    if not equipes:
        # `size` alto para trazer tudo de uma vez: o seletor não pagina.
        r = await _get(TEAMS_PATH, {"size": 200, "page": 1})
        if r.is_success:
            body = r.json()
            equipes = body.get("list", []) if isinstance(body, dict) else []
            logger.info("SGI /teams/ -> %s equipe(s) (de %s)", len(equipes),
                        (body.get("pagination") or {}).get("total") if isinstance(body, dict) else "?")
        else:
            logger.warning("SGI /teams/ respondeu %s: %s", r.status_code, r.text[:200])

    # Só o que a tela usa. `id` e `name` são os únicos campos garantidos pela spec.
    return [{"id": t.get("id"), "name": t.get("name")} for t in equipes if t.get("id")]


async def autoenable_from_env() -> None:
    """Reconecta o SGI no startup quando há credencial no ambiente (só dev).

    Sem isto, cada reload do uvicorn exige refazer o login pela tela — o que
    torna o ciclo de desenvolvimento inviável. Em produção as variáveis ficam
    vazias e esta função não faz nada, preservando a política de credencial
    apenas em RAM, digitada pelo usuário.
    """
    logger.info("SGI base_url em uso: %s", settings.sgi_base_url)
    if is_enabled() or not (settings.sgi_username and settings.sgi_password):
        return
    try:
        await enable(settings.sgi_username, settings.sgi_password)
        logger.info("SGI reconectado pelo ambiente (modo dev).")
    except Exception as exc:  # noqa: BLE001 — credencial de dev errada não derruba o app
        logger.warning("SGI: auto-conexão pelo ambiente falhou: %s", exc)


async def health_loop() -> None:
    """Health check do token: a cada 30 min valida e renova se estiver perto de expirar.

    Sem isto a renovação só aconteceria na próxima consulta — e numa TV que fica
    horas na mesma tela, o primeiro acesso depois de um período parado pegaria um
    token morto e mostraria erro na parede.
    """
    while True:
        await asyncio.sleep(settings.sgi_health_every)
        if not is_enabled():
            continue
        try:
            await valid_token()
        except Exception as exc:  # noqa: BLE001 — health check nunca derruba o serviço
            if _state:
                _state.last_error = str(exc)
            logger.warning("SGI: health check falhou: %s", exc)
