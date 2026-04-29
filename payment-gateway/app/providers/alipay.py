"""Alipay PC web pay (alipay.trade.page.pay) provider.

The vendored python-alipay-sdk is synchronous; we offload calls to a thread."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs

from ..config import Settings
from ..exceptions import ProviderError
from ..payment_method import PaymentMethod
from .base import NotifyResult, PrepayResult

log = logging.getLogger(__name__)

_PROD_GATEWAY = "https://openapi.alipay.com/gateway.do"
_SANDBOX_GATEWAY = "https://openapi.alipaydev.com/gateway.do"
_SUCCESS_STATUSES = frozenset({"TRADE_SUCCESS", "TRADE_FINISHED"})

# Defense-in-depth replay window. Alipay signs `notify_time` (Beijing-time
# string) as part of the body, so by the time we read it the value is
# tamper-evidently authentic. New-api's state machine catches duplicate
# credits already, but rejecting stale notifies at the gateway is cleaner
# and keeps the audit log honest. 10 min covers worst-case alipay retry
# latency without allowing meaningful replay.
_REPLAY_WINDOW_SECONDS = 600
# Alipay timestamps are in UTC+8 ("China Standard Time"). Bake in the offset
# so we don't depend on server timezone configuration.
_BEIJING_TZ = timezone(timedelta(hours=8))


def _read_pem(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


class AlipayProvider:
    METHOD = PaymentMethod.ALIPAY

    def __init__(
        self,
        sdk: Any,
        *,
        gateway_url: str,
        notify_url: str,
        return_url: str,
    ) -> None:
        self._sdk = sdk
        self._gateway_url = gateway_url
        self._notify_url = notify_url
        self._return_url = return_url

    @classmethod
    def from_settings(cls, s: Settings) -> AlipayProvider:
        s.assert_alipay_ready()
        # Imported lazily so tests / disabled deployments don't need the SDK.
        from alipay import AliPay  # type: ignore[import-not-found]

        # PyCryptodome reports malformed PEMs as "Incorrect padding" or
        # "RSA key format is not supported" — neither of which mentions
        # the file path. Wrap so ops can see WHICH key is bad.
        try:
            app_pk = _read_pem(s.alipay_app_private_key_path)
            alipay_pk = _read_pem(s.alipay_public_key_path)
            sdk = AliPay(
                appid=s.alipay_app_id,
                app_notify_url=s.alipay_notify_url,
                app_private_key_string=app_pk,
                alipay_public_key_string=alipay_pk,
                sign_type=s.alipay_sign_type,
                debug=s.alipay_sandbox,
            )
        except Exception as exc:
            raise RuntimeError(
                f"alipay SDK init failed (check PEMs at "
                f"{s.alipay_app_private_key_path}, {s.alipay_public_key_path}): "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        return cls(
            sdk,
            gateway_url=_SANDBOX_GATEWAY if s.alipay_sandbox else _PROD_GATEWAY,
            notify_url=s.alipay_notify_url,
            return_url=s.alipay_return_url,
        )

    async def prepay(
        self,
        *,
        out_trade_no: str,
        amount_fen: int,
        subject: str,
        attach: str,
    ) -> PrepayResult:
        total_amount = format(Decimal(amount_fen) / Decimal(100), ".2f")
        try:
            order_string = await asyncio.to_thread(
                self._sdk.api_alipay_trade_page_pay,
                subject=subject,
                out_trade_no=out_trade_no,
                total_amount=total_amount,
                return_url=self._return_url,
                notify_url=self._notify_url,
                passback_params=attach,
            )
        except Exception as exc:
            raise ProviderError(f"alipay prepay error: {exc!r}") from exc
        return PrepayResult(redirect_url=f"{self._gateway_url}?{order_string}")

    async def parse_notify(
        self,
        *,
        headers: dict[str, str],
        body: bytes,
    ) -> NotifyResult:
        # Alipay async-notify is application/x-www-form-urlencoded.
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProviderError("notify body not utf-8") from exc
        raw = parse_qs(text, keep_blank_values=True, strict_parsing=False)
        data: dict[str, str] = {k: v[0] for k, v in raw.items() if v}

        sign = data.pop("sign", "")
        data.pop("sign_type", None)
        if not sign:
            raise ProviderError("missing alipay sign")
        try:
            ok = await asyncio.to_thread(self._sdk.verify, data, sign)
        except Exception as exc:
            raise ProviderError(f"alipay verify error: {exc!r}") from exc
        if not ok:
            raise ProviderError("alipay sign mismatch")

        status = data.get("trade_status", "")
        if status not in _SUCCESS_STATUSES:
            raise ProviderError(f"alipay non-success status: {status}")

        # Replay-window check. notify_time was just RSA-verified above, so its
        # value is authentic — only a captured-and-replayed legitimate notify
        # could pass this point with a stale timestamp.
        notify_time_str = data.get("notify_time", "")
        if not notify_time_str:
            raise ProviderError("alipay notify_time missing")
        try:
            notify_dt = datetime.strptime(notify_time_str, "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=_BEIJING_TZ
            )
        except ValueError as exc:
            raise ProviderError("alipay notify_time malformed") from exc
        skew = abs(time.time() - notify_dt.timestamp())
        if skew > _REPLAY_WINDOW_SECONDS:
            raise ProviderError(f"alipay notify_time out of window: skew={skew:.0f}s")

        try:
            amount_fen = int(
                (Decimal(data["total_amount"]) * 100).to_integral_value()
            )
        except (KeyError, ArithmeticError, ValueError) as exc:
            raise ProviderError("alipay total_amount missing/invalid") from exc

        return NotifyResult(
            out_trade_no=data.get("out_trade_no", ""),
            provider_trade_no=data.get("trade_no", ""),
            amount_fen=amount_fen,
            # Alipay URL-decodes passback_params before sending it back.
            attach=data.get("passback_params", ""),
            payment_method=PaymentMethod.ALIPAY,
        )
