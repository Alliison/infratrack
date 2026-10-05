"""Sessoes persistidas em disco — TEMPORARIO (autorizado pelo responsavel).

Por padrao credenciais ficam so em RAM. Com TRACKINFRA_PERSIST_SESSIONS=true
este modulo grava, no volume /data, as contas (cookies, token, usuario e senha
do FullTrack), os tokens das TVs e celulares e a credencial do SGI, para um
restart/deploy nao derrubar as TVs no QR nem desconectar o SGI. Nome e config
de cada TV nao estao aqui: vao sempre para devices.json (ver devices.py).

Formato 2 (contas + tokens). O formato 1 (lista solta de sessoes de TV) ainda
e' lido e migrado no restore — e' o que a producao grava hoje.

Contem SENHAS EM TEXTO PURO (arquivo 0600, so no volume). Para limpar tudo:
    docker exec trackinfra rm -f /data/sessions_state.json && docker restart trackinfra
e tirar a variavel do docker-compose.yml para nao voltar a gravar.
"""
import asyncio
import json
import logging
import os
import secrets
from pathlib import Path

from . import devices, sgi
from .config import settings
from .sessions import Account, AppSession, FulltrackAuth, account_key, store

logger = logging.getLogger("trackinfra.persist")
SAVE_EVERY = 30  # s — pega renovacoes de cookie/token feitas por fora


def _path() -> Path:
    return Path(settings.config_path).resolve().parent / "sessions_state.json"


def _snapshot() -> dict:
    accounts = []
    for acc in store._accounts.values():
        a = acc.auth
        accounts.append({"id": acc.id, "created_at": acc.created_at,
                         "cookies": a.cookies, "token": a.token,
                         "token_obtained_at": a.token_obtained_at,
                         "login_user": a.login_user, "password": a.password})
    grants = [{"app_token": g.app_token, "account_id": g.account.id, "kind": g.kind,
               "device_id": g.device_id, "id": g.id, "label": g.label,
               "created_at": g.created_at, "idle_ttl": g.idle_ttl}
              for g in store._app.values()]
    st = sgi._state
    return {"version": 2, "accounts": accounts, "grants": grants,
            "sgi": ({"username": st.username, "password": st.password,
             "totp_secret": st.totp_secret} if st else None)}


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


def _account_from(d: dict) -> Account:
    auth = FulltrackAuth(cookies=d.get("cookies") or {}, token=d.get("token") or {},
                         token_obtained_at=d.get("token_obtained_at", 0),
                         login_user=d.get("login_user"), password=d.get("password"))
    key = d.get("id") or account_key(d.get("login_user") or "")
    acc = store._accounts.get(key)
    if not acc:
        acc = Account(id=key, auth=auth, created_at=d.get("created_at", 0))
        store._accounts[key] = acc
    return acc


def _restore_v1(data: dict) -> None:
    """Formato antigo (antes das contas/dispositivos): uma lista de sessões de
    TV soltas. Cada uma vira uma TV registrada na conta do seu usuário, com a
    configuração global que ela já exibia — a TV nem percebe a migração."""
    for d in data.get("app_sessions", []):
        acc = _account_from(d)
        dev = devices.claim(acc.id, None)
        store._app[d["app_token"]] = AppSession(app_token=d["app_token"], account=acc,
                                                kind="tv", device_id=dev.id,
                                                created_at=d.get("created_at", 0))
    logger.info("sessoes no formato antigo migradas: %d TV(s)", len(data.get("app_sessions", [])))


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
    if data.get("version") != 2:
        _restore_v1(data)
    else:
        for d in data.get("accounts", []):
            _account_from(d)
        n = 0
        for g in data.get("grants", []):
            acc = store._accounts.get(g["account_id"])
            if not acc:
                continue
            store._app[g["app_token"]] = AppSession(
                app_token=g["app_token"], account=acc, kind=g.get("kind", "tv"),
                device_id=g.get("device_id"), id=g.get("id") or secrets.token_urlsafe(8),
                label=g.get("label", ""), created_at=g.get("created_at", 0),
                idle_ttl=g.get("idle_ttl", 0))
            n += 1
        logger.info("%d conta(s), %d token(s) restaurado(s)", len(data.get("accounts", [])), n)
    s = data.get("sgi")
    if s and not sgi.is_enabled():
        try:
            await sgi.enable(s["username"], s["password"], s.get("totp_secret"))
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
