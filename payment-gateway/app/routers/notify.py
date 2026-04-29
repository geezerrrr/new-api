"""Asynchronous-notification endpoints.

Both providers funnel into the same handler:
  1. Provider parses + verifies the notify body (SDK-level crypto + our
     replay-window check).
  2. Open the HMAC `attach`, recover (out_trade_no, money_yuan).
  3. Cross-check that the provider-reported amount and trade_no match what
     we pinned in `attach` at submit time.
  4. Build epay-format params and GET new-api's notify URL.
  5. Only ack the payment platform with success when new-api acknowledges.
     Any failure short-circuits to the platform's "fail" code so the
     platform retries — this is our durability mechanism in lieu of a DB.

Each provider has its own ack format (Alipay text "success"/"fail",
WeChat JSON 200/500), but the verification flow is identical, hence the
shared `_process` helper plus a per-method `_AckPair`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from ..attach import open_attach
from ..epay import build_notify_params, deliver_notify
from ..exceptions import InvalidAttach, ProviderError, UpstreamNotifyFailed
from ..payment_method import PaymentMethod
from ..providers.base import NotifyResult
from ..schemas import money_to_fen

router = APIRouter()
log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _AckPair:
    """Per-provider response factories. Each platform has its own contract
    for what 'success' and 'retry-please' look like."""

    success: Callable[[], Response]
    fail: Callable[[], Response]


# Alipay protocol mandates a literal `success` body (HTTP 200) — anything
# else triggers retry (up to 8 attempts over ~25 hours).
_ALIPAY_ACK = _AckPair(
    success=lambda: PlainTextResponse("success"),
    fail=lambda: PlainTextResponse("fail"),
)

# WeChat APIv3 reads HTTP status: 2xx = success, otherwise retry. We use
# 5xx to signal "transient — please retry" (15 attempts over ~24 hours).
_WECHAT_ACK = _AckPair(
    success=lambda: JSONResponse({"code": "SUCCESS", "message": "OK"}, status_code=200),
    fail=lambda: JSONResponse({"code": "FAIL", "message": "retry"}, status_code=500),
)


async def _process(request: Request, *, method: PaymentMethod, ack: _AckPair) -> Response:
    state = request.app.state.gateway

    provider = state.providers.get(method)
    if provider is None:
        log.warning("notify: %s called but provider disabled", method)
        return ack.fail()

    body = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}

    # Step 1 — provider crypto + replay-window check.
    try:
        result: NotifyResult = await provider.parse_notify(headers=headers, body=body)
    except ProviderError as exc:
        log.warning("notify: %s parse failed: %s", method, exc)
        return ack.fail()

    # Internal consistency: the provider should always tag its own method.
    # A mismatch would indicate registry mis-wiring (impossible today since
    # `state.providers` is keyed by method, but defensive against future
    # refactors that introduce a registry layer).
    if result.payment_method != method:
        log.error(
            "notify: provider/method mismatch route=%s result=%s",
            method, result.payment_method,
        )
        return ack.fail()

    # Step 2 — recover the values we pinned in `attach` at submit time.
    try:
        attach_trade_no, attach_money = open_attach(
            result.attach, secret=state.attach_secret
        )
    except InvalidAttach as exc:
        log.warning("notify: bad attach for %s: %s", result.out_trade_no, exc)
        return ack.fail()

    # Step 3 — cross-check. Either mismatch is a security event: we log loud
    # and return fail (platform retries; same forged data fails again; no
    # quota credit happens).
    if attach_trade_no != result.out_trade_no:
        log.error(
            "notify: trade_no mismatch attach=%s callback=%s",
            attach_trade_no,
            result.out_trade_no,
        )
        return ack.fail()
    try:
        expected_fen = money_to_fen(attach_money)
    except ValueError:
        log.error("notify: corrupt attach money for %s", result.out_trade_no)
        return ack.fail()
    if expected_fen != result.amount_fen:
        log.error(
            "notify: amount mismatch attach=%d callback=%d trade_no=%s",
            expected_fen,
            result.amount_fen,
            result.out_trade_no,
        )
        return ack.fail()

    # Step 4 — forward to new-api in epay format.
    notify_params = build_notify_params(
        pid=state.settings.epay_pid,
        epay_key=state.epay_key,
        trade_no=result.provider_trade_no or result.out_trade_no,
        out_trade_no=result.out_trade_no,
        payment_type=result.payment_method,
        name="topup",  # opaque to new-api — only the signature matters
        money=attach_money,
    )
    try:
        await deliver_notify(
            state.http,
            str(state.settings.new_api_notify_url),
            notify_params,
            timeout=state.settings.new_api_notify_timeout,
        )
    except UpstreamNotifyFailed as exc:
        # CRITICAL: we did NOT credit the user yet. By returning the platform's
        # "retry" code, we lean on its built-in 24h retry to recover. If we
        # incorrectly returned success here, the credit would be lost.
        log.error(
            "notify: upstream failed (will be retried) trade_no=%s err=%s",
            result.out_trade_no,
            exc,
        )
        return ack.fail()

    log.info(
        "notify: ok provider=%s trade_no=%s amount_fen=%d",
        method,
        result.out_trade_no,
        result.amount_fen,
    )
    return ack.success()


@router.post("/alipay/notify")
async def alipay_notify(request: Request) -> Response:
    return await _process(request, method=PaymentMethod.ALIPAY, ack=_ALIPAY_ACK)


@router.post("/wechat/notify")
async def wechat_notify(request: Request) -> Response:
    return await _process(request, method=PaymentMethod.WXPAY, ack=_WECHAT_ACK)
