"""TOTP (RFC 6238) so' com a biblioteca padrao — sem dependencia nova."""
import base64
import hashlib
import hmac
import struct
import time
from typing import Optional
from urllib.parse import parse_qs, urlparse


def clean_secret(raw: str) -> str:
    """Aceita a chave crua ("JBSW Y3DP ...") ou a URI otpauth:// do QR."""
    raw = (raw or "").strip()
    if raw.lower().startswith("otpauth://"):
        raw = (parse_qs(urlparse(raw).query).get("secret") or [""])[0]
    secret = raw.replace(" ", "").replace("-", "").upper().rstrip("=")
    base64.b32decode(secret + "=" * (-len(secret) % 8))   # levanta se invalida
    return secret


def step_of(now: Optional[float] = None, period: int = 30) -> int:
    return int((time.time() if now is None else now) // period)


def code(secret: str, step: int, digits: int = 6) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    mac = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    o = mac[-1] & 0x0F
    n = (struct.unpack(">I", mac[o:o + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(n).zfill(digits)
