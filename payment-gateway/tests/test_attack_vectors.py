"""Adversarial tests: signature forgery, replay, injection, DoS, timing."""

from __future__ import annotations

import pytest

from app.attach import open_attach
from app.epay import sign
from app.exceptions import InvalidAttach
from tests.conftest import TEST_EPAY_KEY, TEST_EPAY_PID

# =============================================================
# 1. Signature forgery
# =============================================================


def test_attacker_cannot_create_valid_request_without_key(client):
    """An attacker who guesses MD5(params) without the key is rejected."""
    import hashlib

    p = {
        "pid": TEST_EPAY_PID, "type": "alipay", "out_trade_no": "FORGED",
        "notify_url": "https://api.example.com/api/user/epay/notify",
        "return_url": "", "name": "X", "money": "10.00",
        "device": "pc", "sign_type": "MD5",
    }
    # MD5 without the key suffix.
    canonical = "&".join(f"{k}={p[k]}" for k in sorted(p) if p[k])
    p["sign"] = hashlib.md5(canonical.encode()).hexdigest()  # noqa: S324
    r = client.post("/submit.php", data=p, follow_redirects=False)
    assert r.status_code == 400


def test_attacker_cannot_replay_with_modified_money(client):
    """A captured legitimate request can't be modified — the sig won't match."""
    p = {
        "pid": TEST_EPAY_PID, "type": "alipay", "out_trade_no": "USR1NOABC",
        "notify_url": "https://api.example.com/api/user/epay/notify",
        "return_url": "", "name": "X", "money": "10.00",
        "device": "pc", "sign_type": "MD5",
    }
    p["sign"] = sign(p, TEST_EPAY_KEY)
    p["money"] = "0.01"  # tamper
    r = client.post("/submit.php", data=p, follow_redirects=False)
    assert r.status_code == 400


def test_attacker_cannot_inject_extra_signed_param(client):
    """Adding an extra k=v changes the canonical string — sig invalidates."""
    p = {
        "pid": TEST_EPAY_PID, "type": "alipay", "out_trade_no": "USR1NOABC",
        "notify_url": "https://api.example.com/api/user/epay/notify",
        "return_url": "", "name": "X", "money": "10.00",
        "device": "pc", "sign_type": "MD5",
    }
    p["sign"] = sign(p, TEST_EPAY_KEY)
    p["evil"] = "evil"
    r = client.post("/submit.php", data=p, follow_redirects=False)
    assert r.status_code == 400


# =============================================================
# 2. Attach forgery
# =============================================================


def test_attacker_cannot_forge_attach_without_secret():
    """The attach token is HMAC'd; forging requires the secret."""
    import base64
    import hashlib

    import pytest

    payload = b"USR1NOABC|0.01"
    fake_tag = hashlib.sha256(payload).digest()[:16]
    token = base64.urlsafe_b64encode(fake_tag + payload).rstrip(b"=").decode()
    with pytest.raises(InvalidAttach):
        open_attach(token, secret=b"any-secret-of-correct-length")


# =============================================================
# 3. Injection / sanitisation
# =============================================================


def test_html_in_subject_is_escaped_in_qrcode_page(client):
    """Subject (`name`) is rendered in HTML — must be escaped."""
    from app.epay import sign as _sign

    p = {
        "pid": TEST_EPAY_PID, "type": "wxpay", "out_trade_no": "USR1NOABC",
        "notify_url": "https://api.example.com/api/user/epay/notify",
        "return_url": "", "name": "<script>alert(1)</script>",
        "money": "10.00", "device": "pc", "sign_type": "MD5",
    }
    p["sign"] = _sign(p, TEST_EPAY_KEY)
    r = client.post("/submit.php", data=p)
    assert r.status_code == 200
    assert b"<script>alert(1)</script>" not in r.content
    assert b"&lt;script&gt;" in r.content


def test_path_traversal_in_out_trade_no_rejected(client):
    p = {
        "pid": TEST_EPAY_PID, "type": "alipay",
        "out_trade_no": "../../../etc/passwd",
        "notify_url": "https://api.example.com/api/user/epay/notify",
        "return_url": "", "name": "X", "money": "10.00",
        "device": "pc", "sign_type": "MD5",
    }
    p["sign"] = sign(p, TEST_EPAY_KEY)
    r = client.post("/submit.php", data=p, follow_redirects=False)
    assert r.status_code == 400


def test_crlf_injection_in_out_trade_no_rejected(client):
    p = {
        "pid": TEST_EPAY_PID, "type": "alipay",
        "out_trade_no": "X\r\nSet-Cookie: evil=1",
        "notify_url": "https://api.example.com/api/user/epay/notify",
        "return_url": "", "name": "X", "money": "10.00",
        "device": "pc", "sign_type": "MD5",
    }
    p["sign"] = sign(p, TEST_EPAY_KEY)
    r = client.post("/submit.php", data=p, follow_redirects=False)
    assert r.status_code == 400


# =============================================================
# 4. Resource exhaustion / DoS
# =============================================================


def test_oversized_request_body_rejected(client):
    huge = "x" * (16 * 1024)  # > MAX_REQUEST_BYTES (8 KB)
    r = client.post(
        "/submit.php",
        content=huge.encode(),
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert r.status_code == 413


@pytest.mark.asyncio
async def test_body_size_limit_middleware_truncates_streaming_body():
    """Direct ASGI-level test: a client that sends a body in many chunks
    without a Content-Length header (e.g., chunked transfer encoding) is
    still bounded."""
    from app.middleware import BodySizeLimitMiddleware

    received_bodies: list[bytes] = []

    async def fake_app(scope, receive, send):
        # Drain the body fully and remember what we got.
        body = b""
        while True:
            msg = await receive()
            if msg["type"] != "http.request":
                break
            body += msg.get("body", b"")
            if not msg.get("more_body"):
                break
        received_bodies.append(body)
        await send(
            {"type": "http.response.start", "status": 200, "headers": []}
        )
        await send({"type": "http.response.body", "body": b"ok"})

    mw = BodySizeLimitMiddleware(fake_app, max_bytes=100)

    chunks = [b"x" * 60, b"x" * 60, b"x" * 60]  # 180 bytes total, no CL
    idx = 0

    async def receive():
        nonlocal idx
        if idx < len(chunks):
            chunk = chunks[idx]
            idx += 1
            return {"type": "http.request", "body": chunk, "more_body": idx < len(chunks)}
        return {"type": "http.disconnect"}

    sent: list = []

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": "/x", "headers": []}
    await mw(scope, receive, send)

    # The downstream app should have seen at most max_bytes (100), not the
    # full 180. We sent 60 bytes (still under), then the next chunk pushed
    # us over, so we got truncated.
    assert len(received_bodies) == 1
    assert len(received_bodies[0]) <= 100


@pytest.mark.asyncio
async def test_body_size_limit_middleware_rejects_oversized_cl_upfront():
    """When Content-Length itself exceeds max_bytes, reject 413 without
    even invoking the downstream app."""
    from app.middleware import BodySizeLimitMiddleware

    invoked = False

    async def fake_app(scope, receive, send):
        nonlocal invoked
        invoked = True

    mw = BodySizeLimitMiddleware(fake_app, max_bytes=100)
    sent: list = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/x",
        "headers": [(b"content-length", b"500")],
    }
    await mw(scope, receive, send)

    assert not invoked
    assert sent[0]["status"] == 413


@pytest.mark.asyncio
async def test_body_size_limit_middleware_rejects_invalid_cl():
    from app.middleware import BodySizeLimitMiddleware

    async def fake_app(scope, receive, send):
        raise AssertionError("should not be called")

    mw = BodySizeLimitMiddleware(fake_app, max_bytes=100)
    sent: list = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/x",
        "headers": [(b"content-length", b"not-a-number")],
    }
    await mw(scope, receive, send)
    assert sent[0]["status"] == 400


def test_extremely_long_out_trade_no_rejected(client):
    p = {
        "pid": TEST_EPAY_PID, "type": "alipay",
        "out_trade_no": "A" * 10_000,
        "notify_url": "https://api.example.com/api/user/epay/notify",
        "return_url": "", "name": "X", "money": "10.00",
        "device": "pc", "sign_type": "MD5",
    }
    p["sign"] = sign(p, TEST_EPAY_KEY)
    r = client.post("/submit.php", data=p, follow_redirects=False)
    # Either 413 (size limit) or 400 (schema). Both are acceptable rejections.
    assert r.status_code in (400, 413)


def test_invalid_content_length_rejected(client):
    r = client.post("/submit.php", data=b"a=b",
                    headers={"content-length": "not-a-number"})
    # Starlette rejects malformed CL with 400 before our middleware sees it,
    # but we make sure the response is not 200/302.
    assert r.status_code != 200
    assert r.status_code != 302


# =============================================================
# 5. Timing-attack resistance for sign verify
# =============================================================


def test_sign_verify_uses_constant_time_compare():
    """Both an all-zero sig and a sig differing only in the last byte
    should take roughly the same time. We don't measure absolute time
    (flaky on CI); we verify the implementation uses hmac.compare_digest
    by inspecting that it imports and calls it."""
    import inspect

    from app import epay

    src = inspect.getsource(epay)
    assert "compare_digest" in src


# =============================================================
# 6. Rate limit (smoke; default is 10000/min in tests)
# =============================================================


def test_log_level_is_actually_applied():
    """Regression: LOG_LEVEL was a silent decoy for two rounds (no handler
    attached → INFO/DEBUG records fell through to logging.lastResort and
    were dropped at WARNING level). Verify here that an INFO record from
    the `app` logger tree actually reaches a handler."""
    import io
    import logging

    from app.config import get_settings, reload_settings_for_tests
    from app.main import _configure_app_logger

    reload_settings_for_tests()
    _configure_app_logger(get_settings())

    captured = io.StringIO()
    capture_handler = logging.StreamHandler(captured)
    capture_handler.setLevel(logging.DEBUG)
    logger = logging.getLogger("app")
    logger.addHandler(capture_handler)
    try:
        logging.getLogger("app.test_marker").info("hello-from-app-logger")
    finally:
        logger.removeHandler(capture_handler)

    assert "hello-from-app-logger" in captured.getvalue()


def test_rate_limiter_is_wired_in(client):
    """The SlowAPI limiter must be installed on the app and its middleware
    in the stack. End-to-end 429 testing is fragile (lifespan/env clashes),
    so we verify the wiring directly."""
    from slowapi import Limiter
    from slowapi.middleware import SlowAPIMiddleware

    app = client.app
    assert isinstance(app.state.limiter, Limiter)
    assert any(
        getattr(m, "cls", None) is SlowAPIMiddleware for m in app.user_middleware
    )
