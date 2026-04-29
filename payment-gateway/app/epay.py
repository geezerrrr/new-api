"""epay protocol: MD5 sign + verify + outbound notify to new-api.

Bit-for-bit compatible with github.com/Calcium-Ion/go-epay v0.0.4:
  - filter out keys "sign", "sign_type", and empty values
  - sort remaining keys lexicographically (string sort, NOT URL-encoded)
  - join as "k1=v1&k2=v2&...&kN=vN" (NO trailing &)
  - append the merchant key, MD5(utf8), lowercase hex
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from collections.abc import Mapping

import httpx

from .exceptions import InvalidEpaySignature, UpstreamNotifyFailed
from .payment_method import PaymentMethod

log = logging.getLogger(__name__)

_SIGN_EXCLUDED_KEYS = frozenset({"sign", "sign_type"})


def _canonical_string(params: Mapping[str, str]) -> str:
    items = sorted(
        (k, v) for k, v in params.items() if k not in _SIGN_EXCLUDED_KEYS and v != ""
    )
    return "&".join(f"{k}={v}" for k, v in items)


def sign(params: Mapping[str, str], key: str) -> str:
    """Compute the epay MD5 signature for `params` using merchant `key`."""
    raw = _canonical_string(params) + key
    return hashlib.md5(raw.encode("utf-8")).hexdigest()  # noqa: S324  -- protocol-mandated


def verify(params: Mapping[str, str], key: str) -> None:
    """Raise InvalidEpaySignature if the signature is missing/wrong.

    Uses constant-time comparison to avoid timing oracles."""
    provided = params.get("sign", "")
    if not provided:
        raise InvalidEpaySignature("missing sign")
    expected = sign(params, key)
    # hmac.compare_digest works on equal-length strings of any kind.
    if not hmac.compare_digest(provided.lower(), expected):
        raise InvalidEpaySignature("sign mismatch")


# ---------------- outbound notify to new-api ----------------

# Status string demanded by go-epay's Verify path; trade_status == TRADE_SUCCESS
# is what triggers quota credit on the new-api side.
TRADE_SUCCESS = "TRADE_SUCCESS"


def build_notify_params(
    *,
    pid: str,
    epay_key: str,
    trade_no: str,
    out_trade_no: str,
    payment_type: PaymentMethod,
    name: str,
    money: str,
) -> dict[str, str]:
    """Assemble + sign the GET params we send to new-api's epay-notify endpoint.

    `payment_type` is typed as PaymentMethod (a StrEnum, so its members are
    plain strings at runtime); statically-typed callers can't accidentally
    pass an arbitrary string here."""
    params: dict[str, str] = {
        "pid": pid,
        "trade_no": trade_no,
        "out_trade_no": out_trade_no,
        "type": payment_type,
        "name": name,
        "money": money,
        "trade_status": TRADE_SUCCESS,
        "sign_type": "MD5",
    }
    params["sign"] = sign(params, epay_key)
    return params


async def deliver_notify(
    client: httpx.AsyncClient,
    notify_url: str,
    params: Mapping[str, str],
    *,
    timeout: float,
) -> None:
    """GET the new-api notify endpoint. Raise UpstreamNotifyFailed if it didn't
    answer with the literal body 'success' (per go-epay protocol)."""
    try:
        resp = await client.get(notify_url, params=dict(params), timeout=timeout)
    except httpx.HTTPError as exc:
        raise UpstreamNotifyFailed(f"http error: {exc!r}") from exc
    body = resp.text.strip().lower()
    if resp.status_code != 200 or body != "success":
        raise UpstreamNotifyFailed(
            f"unexpected response status={resp.status_code} body={body!r}"
        )
