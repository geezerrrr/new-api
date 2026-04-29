"""Protocol + value objects shared by every payment provider.

The provider layer is the only part of the gateway that talks to an upstream
payment platform's SDK. Everything above (routers, attach, epay) interacts
solely through `PrepayResult` and `NotifyResult` value objects, which are
deliberately provider-agnostic — no Alipay or WeChat fields leak out.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..payment_method import PaymentMethod


@dataclass(frozen=True, slots=True)
class PrepayResult:
    """What the provider needs the gateway to do next.

    Exactly one of `redirect_url` / `code_url` is set:
      - `redirect_url`: the user is 302'd to this URL (Alipay PC web pay).
      - `code_url`:     embedded into a QR code page rendered by the gateway
                        (WeChat Native scan-to-pay).
    """

    redirect_url: str | None = None
    code_url: str | None = None


@dataclass(frozen=True, slots=True)
class NotifyResult:
    """Verified asynchronous-notification payload.

    All amounts are in `fen` (int), independent of provider quirks. The
    `attach` is whatever bytes the provider round-tripped through their
    `passback_params` / `attach` field — opaque here, HMAC-decoded by the
    notify router."""

    out_trade_no: str
    provider_trade_no: str
    amount_fen: int
    attach: str
    payment_method: PaymentMethod


class PaymentProvider(Protocol):
    """Async interface every provider must implement.

    Implementations are expected to be stateless w.r.t. orders — the gateway
    keeps no per-order state. Provider instances may hold long-lived SDK
    handles (HTTP clients, key material, platform-cert caches)."""

    async def prepay(
        self,
        *,
        out_trade_no: str,
        amount_fen: int,
        subject: str,
        attach: str,
    ) -> PrepayResult: ...

    async def parse_notify(
        self,
        *,
        headers: dict[str, str],
        body: bytes,
    ) -> NotifyResult: ...
