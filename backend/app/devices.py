"""Registro de dispositivos (TVs): nome e configuração de cada uma.

Gravado sempre em disco (`devices.json`, ao lado do mosaic_config.json) porque
não tem segredo nenhum — só nome, conta dona e o MosaicConfig da TV. O token
que liga uma TV física a um destes registros fica em sessions.py (RAM).

Por que persistir mesmo sem TRACKINFRA_PERSIST_SESSIONS: a TV guarda o próprio
`device_id` no navegador e o manda ao pedir um QR novo. Depois de um restart
(sessões perdidas), relogar a mesma TV na mesma conta devolve o nome e a
configuração dela — ninguém remonta a seleção de carros à toa.

O estado ao vivo (online, tela atual, placas no ar) chega pelo heartbeat da TV
e fica só em RAM: some no restart, e tudo bem — volta no próximo ciclo.
"""
import json
import logging
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import storage
from .config import settings
from .models import MosaicConfig

logger = logging.getLogger("trackinfra.devices")
_lock = threading.Lock()


@dataclass
class Device:
    id: str
    account_id: str
    name: str
    config: MosaicConfig
    created_at: float = field(default_factory=time.time)

    def to_json(self) -> dict:
        return {"id": self.id, "account_id": self.account_id, "name": self.name,
                "config": self.config.model_dump(), "created_at": self.created_at}


@dataclass
class Live:
    """O que a TV disse no último heartbeat. Só RAM."""
    last_seen: float = 0
    ip: str = ""
    user_agent: str = ""
    screen: str = ""          # fulltrack | sgi | qr
    page: str = ""            # rótulo da página, como aparece na barra da TV
    showing: list[str] = field(default_factory=list)   # placas na tela agora
    grid: str = ""
    viewport: str = ""
    version: str = ""
    commands: list[str] = field(default_factory=list)  # pendentes p/ a TV


_devices: dict[str, Device] = {}
_live: dict[str, Live] = {}
_loaded = False


def _path() -> Path:
    return Path(settings.config_path).resolve().parent / "devices.json"


def _load() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    p = _path()
    if not p.exists():
        return
    try:
        for d in json.loads(p.read_text(encoding="utf-8")):
            _devices[d["id"]] = Device(id=d["id"], account_id=d["account_id"], name=d["name"],
                                       config=MosaicConfig(**d.get("config", {})),
                                       created_at=d.get("created_at", 0))
    except Exception as exc:  # noqa: BLE001 — arquivo ruim não derruba o app
        logger.warning("devices.json ilegível, começando vazio: %s", exc)


def _save() -> None:
    p = _path()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps([d.to_json() for d in _devices.values()], indent=2),
                   encoding="utf-8")
    tmp.replace(p)


def get(device_id: Optional[str]) -> Optional[Device]:
    with _lock:
        _load()
        return _devices.get(device_id) if device_id else None


def of_account(account_id: str) -> list[Device]:
    with _lock:
        _load()
        return sorted((d for d in _devices.values() if d.account_id == account_id),
                      key=lambda d: d.created_at)


def _next_name(account_id: str) -> str:
    usados = {d.name for d in _devices.values() if d.account_id == account_id}
    nums = [int(m.group(1)) for n in usados if (m := re.fullmatch(r"TV (\d+)", n))]
    n = max(nums, default=0) + 1
    while f"TV {n}" in usados:
        n += 1
    return f"TV {n}"


def claim(account_id: str, hint: Optional[str], name: Optional[str] = None) -> Device:
    """Dispositivo para uma TV que acabou de ser liberada.

    `hint` é o device_id que a TV lembrava. Só é reaproveitado se for da MESMA
    conta: o id não é segredo, e aceitar de outra conta deixaria alguém herdar
    nome/config de uma TV alheia só por saber o id.

    TV nova nasce com a configuração "da casa" (o mosaic_config.json antigo,
    global) — é o mesmo mosaico que ela exibiria antes desta mudança.
    """
    with _lock:
        _load()
        d = _devices.get(hint) if hint else None
        if d and d.account_id == account_id:
            if name:
                d.name = name
                _save()
            return d
        d = Device(id=secrets.token_urlsafe(9), account_id=account_id,
                   name=(name or "").strip() or _next_name(account_id),
                   config=storage.load_config())
        _devices[d.id] = d
        _save()
        return d


def rename(device_id: str, name: str) -> None:
    with _lock:
        _load()
        d = _devices.get(device_id)
        if d:
            d.name = name.strip()[:60] or d.name
            _save()


def set_config(device_id: str, cfg: MosaicConfig) -> MosaicConfig:
    with _lock:
        _load()
        d = _devices.get(device_id)
        if d:
            d.config = cfg
            _save()
        return cfg


def forget(device_id: str) -> None:
    with _lock:
        _load()
        _devices.pop(device_id, None)
        _live.pop(device_id, None)
        _save()


# ---- estado ao vivo -------------------------------------------------------
def live(device_id: str) -> Live:
    return _live.setdefault(device_id, Live())


def heartbeat(device_id: str, data: dict, ip: str, ua: str) -> list[str]:
    """Registra o que a TV está mostrando e devolve os comandos pendentes."""
    lv = live(device_id)
    lv.last_seen = time.time()
    lv.ip, lv.user_agent = ip, ua[:200]
    for k in ("screen", "page", "grid", "viewport", "version"):
        if k in data:
            setattr(lv, k, str(data[k])[:120])
    if isinstance(data.get("showing"), list):
        lv.showing = [str(x)[:20] for x in data["showing"][:64]]
    cmds, lv.commands = lv.commands, []
    return cmds


def push_command(device_id: str, cmd: str) -> None:
    lv = live(device_id)
    if cmd not in lv.commands:
        lv.commands.append(cmd)
