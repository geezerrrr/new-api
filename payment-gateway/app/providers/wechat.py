"""WeChat Pay APIv3 Native (scan-to-pay) provider.

Built on `wechatpayv3`, which handles RSA signing, AES-GCM decrypt and
platform-cert auto-rotation."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from ..config import Settings
from ..exceptions import ProviderError
from ..payment_method import PaymentMethod
from .base import NotifyResult, PrepayResult

log = logging.getLogger(__name__)

# Defense-in-depth replay window: refuse any callback whose Wechatpay-Timestamp
# is more than this many seconds away from `now`. wechatpayv3's RSA verification
# proves authenticity but does NOT validate timestamp freshness; without this
# check, a captured legitimate notify could be replayed indefinitely. The
# new-api state machine still catches duplicate credits, but rejecting at the
# gateway boundary is cleaner. 5 min is comfortably above wechat's normal
# end-to-end latency (~seconds) and below useful attack windows.
_REPLAY_WINDOW_SECONDS = 300


def _read_pem(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


class WechatProvider:
    METHOD = PaymentMethod.WXPAY

    def __init__(self, sdk: Any, *, notify_url: str, pay_type: Any = None) -> None:
        self._sdk = sdk
        self._notify_url = notify_url
        # `pay_type` is the WeChatPayType enum value used by the SDK to pick
        # the right unified-order endpoint. Stored explicitly so prepay() never
        # falls back to the constructor's default (which a future SDK might
        # change). None is allowed in tests with a stub SDK.
        self._pay_type = pay_type

    @classmethod
    def from_settings(cls, s: Settings) -> WechatProvider:
        s.assert_wechat_ready()
        from wechatpayv3 import WeChatPay, WeChatPayType  # type: ignore[import-not-found]

        # SDK init does several things that can fail with cryptic errors:
        # - parse merchant private key (PyCryptodome quirk: "Incorrect padding")
        # - HTTP-fetch platform certs from api.mch.weixin.qq.com
        # Wrap so the failure tells ops which side broke.
        try:
            sdk = WeChatPay(
                wechatpay_type=WeChatPayType.NATIVE,
                mchid=s.wechat_mch_id,
                private_key=_read_pem(s.wechat_mch_private_key_path),
                cert_serial_no=s.wechat_mch_cert_serial_no,
                apiv3_key=s.wechat_apiv3_key,
                appid=s.wechat_app_id,
                notify_url=s.wechat_notify_url,
                cert_dir=s.wechat_cert_dir or None,
                # MUST be set: SDK's default `None` translates to `requests`'
                # "block forever", which would pin asyncio executor threads
                # and freeze the gateway on any WeChat-side outage.
                timeout=(s.wechat_connect_timeout, s.wechat_read_timeout),
            )
        except Exception as exc:
            raise RuntimeError(
                f"wechat SDK init failed (check PEM at "
                f"{s.wechat_mch_private_key_path}, cert_dir at {s.wechat_cert_dir}, "
                f"and connectivity to api.mch.weixin.qq.com): "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        return cls(sdk, notify_url=s.wechat_notify_url, pay_type=WeChatPayType.NATIVE)

    async def prepay(
        self,
        *,
        out_trade_no: str,
        amount_fen: int,
        subject: str,
        attach: str,
    ) -> PrepayResult:
        kwargs: dict[str, Any] = {
            "description": subject,
            "out_trade_no": out_trade_no,
            "amount": {"total": amount_fen, "currency": "CNY"},
            "attach": attach,
            "notify_url": self._notify_url,
        }
        if self._pay_type is not None:
            kwargs["pay_type"] = self._pay_type
        try:
            code, message = await asyncio.to_thread(self._sdk.pay, **kwargs)
        except Exception as exc:
            raise ProviderError(f"wechat prepay error: {exc!r}") from exc
        if code != 200:
            raise ProviderError(f"wechat prepay http={code} msg={message!r}")
        try:
            payload = json.loads(message) if isinstance(message, str) else message
        except json.JSONDecodeError as exc:
            raise ProviderError("wechat prepay non-json response") from exc
        code_url = payload.get("code_url")
        if not code_url or not code_url.startswith("weixin://"):
            raise ProviderError("wechat prepay missing code_url")
        return PrepayResult(code_url=code_url)

    async def parse_notify(
        self,
        *,
        headers: dict[str, str],
        body: bytes,
    ) -> NotifyResult:
        # 1. Replay-window check (cheap; before RSA op).
        ts_header = headers.get("wechatpay-timestamp", "")
        try:
            ts = int(ts_header)
        except ValueError as exc:
            raise ProviderError("wechat missing/invalid timestamp") from exc
        skew = abs(time.time() - ts)
        if skew > _REPLAY_WINDOW_SECONDS:
            raise ProviderError(f"wechat timestamp out of window: skew={skew:.0f}s")

        # 2. SDK-level signature + AES-GCM decryption.
        try:
            result = await asyncio.to_thread(
                self._sdk.callback, headers=headers, body=body
            )
        except Exception as exc:
            raise ProviderError(f"wechat callback parse error: {exc!r}") from exc
        if not result:
            raise ProviderError("wechat callback verify failed")

        event_type = result.get("event_type", "")
        if event_type != "TRANSACTION.SUCCESS":
            raise ProviderError(f"wechat unexpected event: {event_type}")

        resource = result.get("resource") or {}
        if isinstance(resource, str):
            try:
                resource = json.loads(resource)
            except json.JSONDecodeError as exc:
                raise ProviderError("wechat resource not json") from exc

        if resource.get("trade_state") != "SUCCESS":
            raise ProviderError(f"wechat trade_state={resource.get('trade_state')!r}")

        amount = resource.get("amount") or {}
        amount_fen = amount.get("total")
        if not isinstance(amount_fen, int) or amount_fen <= 0:
            raise ProviderError("wechat amount.total missing/invalid")

        out_trade_no = resource.get("out_trade_no") or ""
        if not out_trade_no:
            raise ProviderError("wechat out_trade_no missing")

        return NotifyResult(
            out_trade_no=out_trade_no,
            provider_trade_no=resource.get("transaction_id", ""),
            amount_fen=amount_fen,
            attach=resource.get("attach", ""),
            payment_method=PaymentMethod.WXPAY,
        )
