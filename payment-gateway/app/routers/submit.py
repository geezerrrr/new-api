"""POST/GET /submit.php — epay-protocol entry point.

Flow:
  1. Parse form (POST) or query (GET) into a dict[str,str].
  2. Verify epay MD5 signature with the configured EPAY_KEY.
  3. Schema-validate (whitelist `type`, sanity-check `money`).
  4. Build HMAC-protected `attach` and call the upstream provider.
  5. Redirect (Alipay) or render a QR-code page (WeChat).

The path `/submit.php` is hard-coded by github.com/Calcium-Ion/go-epay
(legacy easyepay PHP convention); it has no PHP runtime relevance.
"""

from __future__ import annotations

import asyncio
import html
import io
import logging
from base64 import b64encode

import qrcode
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError

from ..attach import make_attach
from ..epay import verify
from ..exceptions import (
    GatewayError,
    InvalidEpaySignature,
    ProviderDisabled,
    ProviderError,
)
from ..schemas import EpaySubmitParams, money_to_fen

router = APIRouter()
log = logging.getLogger(__name__)

_QR_HTML_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>WeChat Pay</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
 body{{font-family:-apple-system,Segoe UI,sans-serif;background:#fafafa;
   display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}}
 .card{{background:#fff;border-radius:12px;padding:32px;box-shadow:0 8px 30px rgba(0,0,0,.08);text-align:center;max-width:360px}}
 h1{{font-size:18px;margin:0 0 16px;color:#222}}
 img{{width:240px;height:240px;display:block;margin:0 auto 16px}}
 p{{font-size:13px;color:#666;margin:8px 0}}
 .amt{{font-size:28px;font-weight:600;color:#07c160;margin:8px 0}}
</style></head><body>
<div class="card">
  <h1>{subject}</h1>
  <div class="amt">¥ {amount_yuan}</div>
  <img src="data:image/png;base64,{qr_b64}" alt="QR code">
  <p>请使用微信扫描二维码完成支付</p>
  <p>订单号 {out_trade_no}</p>
</div></body></html>
"""


def _render_qr_page(
    *, code_url: str, subject: str, amount_yuan: str, out_trade_no: str
) -> str:
    img = qrcode.make(code_url, box_size=8, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    qr_b64 = b64encode(buf.getvalue()).decode("ascii")
    return _QR_HTML_TEMPLATE.format(
        subject=html.escape(subject),
        amount_yuan=html.escape(amount_yuan),
        qr_b64=qr_b64,
        out_trade_no=html.escape(out_trade_no),
    )


async def _params_from_request(request: Request) -> dict[str, str]:
    if request.method == "POST":
        # FastAPI parses form data with starlette; size already capped by middleware.
        form = await request.form()
        return {k: str(v) for k, v in form.items()}
    return dict(request.query_params)


@router.api_route("/submit.php", methods=["GET", "POST"])
async def submit(request: Request) -> Response:
    state = request.app.state.gateway
    raw_params = await _params_from_request(request)

    # Step 1 — epay signature. Done BEFORE pydantic validation so a forged
    # request gets rejected with a generic "invalid signature" rather than
    # a detailed schema error that leaks accepted field names.
    try:
        verify(raw_params, state.epay_key)
    except InvalidEpaySignature as exc:
        log.warning("submit: bad epay sign from %s: %s", request.client, exc)
        return Response("invalid signature", status_code=400)

    # Defense in depth — pid is already covered by the signature, but a
    # mismatched pid can only mean a misconfigured client and we want a
    # specific log signal for that case rather than burying it in "bad sign".
    if raw_params.get("pid") != state.settings.epay_pid:
        return Response("invalid pid", status_code=400)

    # Step 2 — structural validation.
    try:
        params = EpaySubmitParams(**raw_params)
    except ValidationError:
        return Response("invalid params", status_code=400)

    # Step 3 — provider routing.
    provider = state.providers.get(params.type)
    if provider is None:
        raise ProviderDisabled(public=f"{params.type} not enabled")

    # Step 4 — dispatch. The HMAC-signed `attach` is what lets the notify
    # handler later reject any callback whose amount/trade_no doesn't match
    # what we authorised here, without a database.
    attach = make_attach(
        out_trade_no=params.out_trade_no,
        money=params.money,
        secret=state.attach_secret,
    )
    amount_fen = money_to_fen(params.money)

    try:
        result = await provider.prepay(
            out_trade_no=params.out_trade_no,
            amount_fen=amount_fen,
            subject=params.name,
            attach=attach,
        )
    except GatewayError:
        # Already public-message-safe; let the global handler emit a 400.
        raise
    except Exception as exc:  # noqa: BLE001 — masked into ProviderError
        # Don't put `exc!r` into the new exception's message: third-party SDK
        # exception reprs can carry HTTP response excerpts / partial tokens
        # which would then leak into the gateway_error_handler's log line.
        # The full chain is preserved via `from exc` and emitted by the
        # log.exception call below — that goes to the server log only.
        log.exception("provider crash trade_no=%s", params.out_trade_no)
        raise ProviderError("unexpected provider error") from exc

    if result.redirect_url:
        return RedirectResponse(result.redirect_url, status_code=302)
    if result.code_url:
        # QR encoding + PNG compression are CPU-bound (~50ms). Off-load to a
        # thread so a single render does not stall other requests.
        page = await asyncio.to_thread(
            _render_qr_page,
            code_url=result.code_url,
            subject=params.name,
            amount_yuan=params.money,
            out_trade_no=params.out_trade_no,
        )
        return HTMLResponse(page)
    raise ProviderError("provider returned no redirect or code_url")
