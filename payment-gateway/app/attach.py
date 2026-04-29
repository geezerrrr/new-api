"""HMAC-signed `attach` field used to round-trip the order amount through
Alipay (passback_params) and WeChat Pay (attach), without any local DB.

Layout (binary, before base64url):
    [16 bytes HMAC-SHA256(SECRET, payload)][payload bytes]

`payload` is `out_trade_no|money_yuan` (UTF-8). 16-byte truncated HMAC gives
~2^128 forgery resistance which is overkill for a one-time-use bearer; the
short length keeps us well inside WeChat's 128-char attach limit.
"""

from __future__ import annotations

import base64
import binascii
import hmac
from hashlib import sha256

from .exceptions import InvalidAttach

_HMAC_LEN = 16
_SEP = "|"


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _mac(secret: bytes, payload: bytes) -> bytes:
    return hmac.new(secret, payload, sha256).digest()[:_HMAC_LEN]


def make_attach(*, out_trade_no: str, money: str, secret: bytes) -> str:
    if _SEP in out_trade_no or _SEP in money:
        raise ValueError("attach fields must not contain separator")
    payload = f"{out_trade_no}{_SEP}{money}".encode()
    tag = _mac(secret, payload)
    return _b64url_encode(tag + payload)


def open_attach(token: str, *, secret: bytes) -> tuple[str, str]:
    """Return (out_trade_no, money). Raise InvalidAttach on any tampering."""
    if not token or len(token) > 256:
        raise InvalidAttach("attach length out of range")
    try:
        raw = _b64url_decode(token)
    except (ValueError, binascii.Error) as exc:
        raise InvalidAttach("attach not base64url") from exc
    if len(raw) <= _HMAC_LEN:
        raise InvalidAttach("attach truncated")
    tag, payload = raw[:_HMAC_LEN], raw[_HMAC_LEN:]
    expected = _mac(secret, payload)
    if not hmac.compare_digest(tag, expected):
        raise InvalidAttach("attach mac mismatch")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InvalidAttach("attach payload not utf-8") from exc
    parts = text.split(_SEP)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise InvalidAttach("attach payload malformed")
    return parts[0], parts[1]
