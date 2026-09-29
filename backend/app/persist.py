"""Sessoes persistidas em disco — TEMPORARIO (autorizado pelo responsavel).

Por padrao (e no dev) credenciais ficam so em RAM. Com
TRACKINFRA_PERSIST_SESSIONS=true este modulo grava, no volume /data, as
sessoes da TV (cookies, token, usuario e senha do FullTrack) e a credencial do
SGI, para um restart/deploy nao derrubar a TV no QR nem desconectar o SGI.

Contem SENHAS EM TEXTO PURO (arquivo 0600, so no volume). Para limpar tudo:
    docker exec trackinfra rm -f /data/sessions_state.json && docker restart trackinfra
e tirar a variavel do docker-compose.yml para nao voltar a gravar.
"""
import asyncio
import json
import logging
import os
from pathlib import Path

from . import sgi
from .config import settings
from .sessions import AppSession, FulltrackAuth, store

logger = logging.getLogger("trackinfra.persist")
SAVE_EVERY = 30  # s — pega renovacoes de cookie/token feitas por fora


def _path() -> Path:
    return Path(settings.config_path).resolve().parent / "sessions_state.json"


def _snapshot() -> dict:
    apps = []
    for s in store._app.values():
        if (s.ttl or settings.app_token_ttl) > 0:   # tokens curtos do celular nao
            continue
        a = s.auth
        apps.append({"app_token": s.app_token, "created_at": s.created_at,
                     "cookies": a.cookies, "token": a.token,
                     "token_obtained_at": a.token_obtained_at,
                     "login_user": a.login_user, "password": a.password})
    st = sgi._state
    return {"app_sessions": apps,
            "sgi": {"username": st.username, "password": st.password} if st else None}


_last = ""


def save(force: bool = False) -> None:
    global _last
    if not settings.persist_sessions:
        return
    data = json.dumps(_snapshot(), sort_keys=True)
    if data == _last and not force:
        return
    p = _path()
    tmp = p.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(data)
    os.replace(tmp, p)
    _last = data


async def restore() -> None:
    if not settings.persist_sessions:
        return
    p = _path()
    if not p.exists():
        return
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 — arquivo ruim nao derruba o app
        logger.warning("sessoes salvas ilegiveis, ignorando: %s", exc)
        return
    for d in data.get("app_sessions", []):
        auth = FulltrackAuth(cookies=d["cookies"], token=d["token"],
                             token_obtained_at=d.get("token_obtained_at", 0),
                             login_user=d.get("login_user"), password=d.get("password"))
        store._app[d["app_token"]] = AppSession(app_token=d["app_token"], auth=auth,
                                                created_at=d.get("created_at", 0))
    logger.info("%d sessao(oes) da TV restaurada(s)", len(data.get("app_sessions", [])))
    s = data.get("sgi")
    if s and not sgi.is_enabled():
        try:
            await sgi.enable(s["username"], s["password"])
            logger.info("SGI reconectado com a credencial salva.")
        except Exception as exc:  # noqa: BLE001
            logger.warning("SGI: reconexao com a credencial salva falhou: %s", exc)


async def save_loop() -> None:
    while True:
        await asyncio.sleep(SAVE_EVERY)
        try:
            save()
        except Exception as exc:  # noqa: BLE001
            logger.warning("nao consegui gravar as sessoes: %s", exc)
