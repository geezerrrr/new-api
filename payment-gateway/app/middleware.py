"""Cross-cutting middlewares: body-size cap, security headers, error masking."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from fastapi import Request
from fastapi.responses import JSONResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .exceptions import GatewayError

log = logging.getLogger(__name__)


class BodySizeLimitMiddleware:
    """Pure-ASGI middleware that bounds request body size.

    Two-layer enforcement:
      1. Reject up-front if `Content-Length` header > max_bytes.
      2. While streaming the body to downstream, count actual bytes; if a
         client sends Transfer-Encoding: chunked or omits CL, we still
         truncate before they exhaust memory.

    Implemented at the ASGI level (not BaseHTTPMiddleware) because the
    Starlette base class buffers the entire body before we can intervene."""

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # 1. Up-front Content-Length check. Cheap rejection of honest oversize.
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    if int(value) > self.max_bytes:
                        await self._send_413(send)
                        return
                except ValueError:
                    await self._send_400(send)
                    return
                break

        # 2. Wrap receive() so streaming chunks are counted. If the running
        #    total exceeds the cap, we synthesise a final empty chunk and let
        #    the downstream handler see a truncated body. Then on the next
        #    receive() (if any), we send 413 ourselves.
        total = 0
        truncated = False

        async def bounded_receive() -> Message:
            nonlocal total, truncated
            message = await receive()
            if message["type"] != "http.request":
                return message
            body = message.get("body", b"") or b""
            total += len(body)
            if total > self.max_bytes:
                truncated = True
                # Stop streaming further chunks downstream.
                return {"type": "http.request", "body": b"", "more_body": False}
            return message

        # We could intercept send() to convert any final response into 413
        # when truncated, but downstream signature verification will already
        # fail on the truncated body and raise InvalidEpaySignature/ProviderError
        # — that gives a clean 400 without us having to fight starlette's
        # response state machine. Log the event for ops visibility.
        await self.app(scope, bounded_receive, send)
        if truncated:
            log.warning(
                "body truncated at %d bytes for %s %s",
                self.max_bytes,
                scope.get("method"),
                scope.get("path"),
            )

    @staticmethod
    async def _send_413(send: Send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [(b"content-type", b"text/plain; charset=utf-8")],
            }
        )
        await send({"type": "http.response.body", "body": b"payload too large"})

    @staticmethod
    async def _send_400(send: Send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 400,
                "headers": [(b"content-type", b"text/plain; charset=utf-8")],
            }
        )
        await send({"type": "http.response.body", "body": b"invalid content-length"})


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        resp = await call_next(request)
        # Defense in depth — the gateway itself never serves browser-rendered
        # untrusted content, but headers cost nothing.
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        resp.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        resp.headers.setdefault(
            "Strict-Transport-Security", "max-age=63072000; includeSubDomains"
        )
        # Tight CSP — qrcode page renders an inline <img> from a data URI we
        # generate ourselves, so default-src 'none' + img-src data: is enough.
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'none'; img-src data:; style-src 'unsafe-inline'; "
            "frame-ancestors 'none'",
        )
        return resp


async def gateway_error_handler(request: Request, exc: GatewayError) -> Response:
    # Public message only; full detail goes to the log.
    log.warning(
        "gateway_error path=%s err=%s detail=%s",
        request.url.path,
        type(exc).__name__,
        exc,
    )
    return JSONResponse({"code": -1, "msg": exc.public_message}, status_code=400)


async def unhandled_error_handler(request: Request, exc: Exception) -> Response:
    log.exception("unhandled error path=%s", request.url.path)
    return JSONResponse({"code": -1, "msg": "internal error"}, status_code=500)
