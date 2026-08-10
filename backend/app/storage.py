import json
import threading
from pathlib import Path

from .config import settings
from .models import MosaicConfig

_lock = threading.Lock()


def _path() -> Path:
    return Path(settings.config_path)


def load_config() -> MosaicConfig:
    p = _path()
    if not p.exists():
        return MosaicConfig()
    try:
        return MosaicConfig(**json.loads(p.read_text(encoding="utf-8")))
    except Exception:
        return MosaicConfig()


def save_config(cfg: MosaicConfig) -> MosaicConfig:
    with _lock:
        _path().write_text(cfg.model_dump_json(indent=2), encoding="utf-8")
    return cfg
