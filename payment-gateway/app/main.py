"""FastAPI application entry. Wires settings, providers, middleware, routes."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address

from .config import Settings, get_settings
from .deps import AppState
from .exceptions import GatewayError
from .middleware import (
    BodySizeLimitMiddleware,
    SecurityHeadersMiddleware,
    gateway_error_handler,
    unhandled_error_handler,
)
from .payment_method import PaymentMethod
from .providers.base import PaymentProvider
from .routers.notify import router as notify_router
from .routers.submit import router as submit_router

log = logging.getLogger(__name__)


def _configure_app_logger(s: Settings) -> None:
    """Wire up the `app` logger tree so LOG_LEVEL actually takes effect.

    uvicorn configures its own (`uvicorn`, `uvicorn.error`, `uvicorn.access`)
    loggers but never touches root. Without an explicit handler on `app`,
    records propagate to root, find no handlers there, and fall through to
    `logging.lastResort` — a hard-coded WARNING-level stderr handler — which
    silently drops every INFO and DEBUG record. So just setting the level
    is not enough; we also need a handler.

    Idempotent: safe to call multiple times (e.g. test setup builds many
    apps in one process)."""
    logger = logging.getLogger("app")
    logger.setLevel(s.log_level)
    if not any(getattr(h, "_payment_gateway", False) for h in logger.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S%z",
            )
        )
        # Tag the handler so we can identify it on re-entry without removing
        # handlers a third party may have legitimately added.
        handler._payment_gateway = True  # type: ignore[attr-defined]
        logger.addHandler(handler)
        # propagate=False prevents records from also being printed via root's
        # `lastResort` handler at WARNING+ (which would double-emit those).
        logger.propagate = False


def _build_providers(settings: Settings) -> dict[PaymentMethod, PaymentProvider]:
    """Construct one provider instance per enabled method.

    Imports are local so that a deployment with only Alipay enabled never
    touches the WeChat SDK code path (and vice versa) — useful in tests and
    in environments where one of the SDKs has unmet system deps."""
    providers: dict[PaymentMethod, PaymentProvider] = {}
    if settings.alipay_enabled:
        from .providers.alipay import AlipayProvider

        providers[PaymentMethod.ALIPAY] = AlipayProvider.from_settings(settings)
    if settings.wechat_enabled:
        from .providers.wechat import WechatProvider

        providers[PaymentMethod.WXPAY] = WechatProvider.from_settings(settings)
    return providers


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    http = httpx.AsyncClient(
        timeout=settings.new_api_notify_timeout,
        # Tight pool — outbound calls are sparse (only on payments).
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=5),
        # Disable redirects — new-api's notify endpoint is a fixed URL.
        follow_redirects=False,
        # Do NOT silently route through env proxies (HTTP_PROXY/ALL_PROXY/etc).
        # Payment notifications must take an explicit, predictable path.
        trust_env=False,
    )
    app.state.gateway = AppState(
        settings=settings,
        http=http,
        providers=_build_providers(settings),
        attach_secret=settings.attach_hmac_key.encode("utf-8"),
        epay_key=settings.epay_key,
    )
    try:
        yield
    finally:
        await http.aclose()


def create_app(settings: Settings | None = None) -> FastAPI:
    s = settings or get_settings()
    _configure_app_logger(s)

    app = FastAPI(
        title="payment-gateway",
        version="0.1.0",
        # Disable docs in case the gateway is exposed publicly — payment
        # endpoints + signing logic shouldn't be documented to attackers.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=_lifespan,
    )
    app.state.settings = s

    # ---- middlewares (order: outermost first) ----
    limiter = Limiter(
        key_func=get_remote_address,
        default_limits=[f"{s.rate_limit_per_minute}/minute"],
    )
    app.state.limiter = limiter
    app.add_middleware(SlowAPIMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=s.max_request_bytes)

    # ---- error handlers ----
    app.add_exception_handler(GatewayError, gateway_error_handler)
    app.add_exception_handler(RateLimitExceeded, _ratelimit_handler)
    app.add_exception_handler(Exception, unhandled_error_handler)

    # ---- routes ----
    app.include_router(submit_router)
    app.include_router(notify_router)

    # Operational endpoints — kept off the dedicated routers to avoid
    # confusing them with payment endpoints.
    @app.get("/healthz", include_in_schema=False)
    async def _healthz() -> Response:
        return PlainTextResponse("ok")

    @app.get("/alipay/return", include_in_schema=False)
    async def _alipay_return() -> Response:
        # Browser-redirect destination after PC pay. We do NOT trust this for
        # state changes — only the async notify counts. The user-facing copy
        # is intentionally agnostic about success vs failure since the async
        # notify may not have arrived yet.
        return PlainTextResponse("Payment processed. You can close this window.")

    return app


async def _ratelimit_handler(request: Request, exc: RateLimitExceeded) -> Response:
    return JSONResponse({"code": -1, "msg": "rate limited"}, status_code=429)


app = create_app()
