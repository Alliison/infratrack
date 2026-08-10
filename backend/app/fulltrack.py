"""Cliente do FullTrack (engenharia reversa do front oficial).

Fluxo de login (idêntico ao navegador):
  1. POST /emp/<slug>   com login/password  -> cria cookie de sessão (gesession)
  2. POST /token/Api_ftk4                    -> troca a sessão por token OAuth ftk4
Dados da frota:
  POST /mapaGeral_v2/getDados  (usa o cookie de sessão)  -> JSON { status, dados[] }
API nova (notificações):
  GET  /plataform/notification/total  (Authorization: Bearer <access_token>)
"""
import httpx

from .config import settings
from .models import Vehicle
from .sessions import FulltrackAuth


class AuthError(Exception):
    """Credenciais inválidas ou login recusado."""


class SessionExpired(Exception):
    """A sessão do FullTrack expirou — é preciso refazer o login (novo QR)."""


_BROWSER = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
}
_HEADERS = {**_BROWSER, "X-Requested-With": "XMLHttpRequest"}


async def login(user: str, password: str) -> FulltrackAuth:
    """Executa o login no FullTrack e devolve cookies + token."""
    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as c:
        # 1) GET inicial para estabelecer o cookie de sessão (gesession/slug)
        await c.get(settings.emp_login_url, headers=_BROWSER)

        # 2) POST das credenciais (charset iso-8859-1 como o form oficial).
        #    Sucesso = redireciona para /dashboard_controller.
        form = f"login={_enc(user)}&password={_enc(password)}"
        r = await c.post(
            settings.emp_login_url,
            content=form.encode("iso-8859-1"),
            headers={**_BROWSER, "Content-Type": "application/x-www-form-urlencoded",
                     "Referer": settings.emp_login_url},
        )
        if "dashboard" not in str(r.url):
            raise AuthError("Usuário ou senha inválidos.")
        cookies = {k: v for k, v in c.cookies.items()}

        # 3) troca a sessão autenticada por um token OAuth ftk4
        t = await c.post(f"{settings.fulltrack_base}/token/Api_ftk4", headers=_HEADERS)
        token = _safe_json(t)
        if not token or "access_token" not in token:
            raise AuthError("Usuário ou senha inválidos.")

        return FulltrackAuth(cookies=cookies, token=token)


async def refresh_token(auth: FulltrackAuth) -> None:
    """Renova o token ftk4 via refresh_token (não precisa de senha)."""
    refresh = auth.token.get("refresh_token")
    if not refresh:
        return
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(
            f"{settings.fulltrack_api_base}/token/refresh",
            data={"grant_type": "refresh_token", "refresh_token": refresh},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        token = _safe_json(r)
        if token and "access_token" in token:
            auth.token = token


async def get_fleet(auth: FulltrackAuth) -> list[Vehicle]:
    """Busca a frota inteira via getDados (backend legado, cookie de sessão)."""
    async with httpx.AsyncClient(timeout=30, cookies=auth.cookies) as c:
        r = await c.post(
            f"{settings.fulltrack_base}/mapaGeral_v2/getDados",
            headers={**_HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
        )
    data = _safe_json(r)
    # Se a sessão caiu, o FullTrack devolve o HTML de login em vez de JSON.
    if not data or "dados" not in data:
        raise SessionExpired()
    return [_normalize(v) for v in data.get("dados", [])]


async def get_notifications_total(auth: FulltrackAuth) -> int:
    """Contador de alertas não lidos (API nova, com auto-refresh em 401)."""
    for attempt in (1, 2):
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(
                f"{settings.fulltrack_api_base}/plataform/notification/total",
                headers={"Authorization": f"Bearer {auth.token.get('access_token')}"},
            )
        if r.status_code == 401 and attempt == 1:
            await refresh_token(auth)
            continue
        j = _safe_json(r) or {}
        return int(j.get("total_unread", 0))
    return 0


# --------------------------------------------------------------------------
def _enc(v: str) -> str:
    from urllib.parse import quote
    return quote(v, safe="")


def _safe_json(r: httpx.Response):
    try:
        return r.json()
    except Exception:
        # getDados vem como text/html mas com corpo JSON — tenta manualmente
        try:
            import json
            return json.loads(r.text.strip())
        except Exception:
            return None


def _s(x):
    """Devolve string não-vazia ou None (o FullTrack às vezes manda False)."""
    return x if isinstance(x, str) and x.strip() else None


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _normalize(v: dict) -> Vehicle:
    loc = v.get("loc") or [None, None]
    vei = v.get("ras_veiculos") or {}
    mot = v.get("ras_motoristas") or {}
    ident = v.get("ras_eve_id_aparelho") or (v.get("_id") or {}).get("$id") or ""
    ref = _s(v.get("ras_eve_dis_ponto_referencia"))
    if ref == "no_nearby_landmarks":
        ref = None
    return Vehicle(
        id=str(ident),
        placa=_s(vei.get("ras_vei_placa")),
        modelo=_s(vei.get("ras_vei_modelo")),
        cor=_s(vei.get("ras_vei_cor")),
        lat=loc[0] if len(loc) > 0 else None,
        lng=loc[1] if len(loc) > 1 else None,
        velocidade=_f(v.get("ras_eve_velocidade")),
        ignicao=bool(v.get("ras_eve_ignicao")),
        data_gps=_s(v.get("ras_eve_data_gps")),
        motorista=_s(mot.get("ras_mot_nome")),
        ref_proxima=ref,
    )
