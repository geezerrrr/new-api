"""Shared fixtures: env vars, app factory, fake providers."""

from __future__ import annotations

import os
from collections.abc import Generator
from dataclasses import dataclass, field
from typing import Any

import pytest
from fastapi.testclient import TestClient

# Stable test secrets — never use these in production. (32+ chars to satisfy
# the production-grade min_length validator.)
TEST_EPAY_PID = "1000"
TEST_EPAY_KEY = "abcdefghijklmnopqrstuvwxyz0123456789ABCDEF"  # 42 chars
TEST_ATTACH_KEY = "0123456789abcdef0123456789abcdef0123456789"  # 42 chars
TEST_NOTIFY_URL = "https://api.example.com/api/user/epay/notify"


def _set_env() -> None:
    os.environ.update(
        {
            "GATEWAY_PUBLIC_URL": "https://pay.example.com",
            "EPAY_PID": TEST_EPAY_PID,
            "EPAY_KEY": TEST_EPAY_KEY,
            "ATTACH_HMAC_KEY": TEST_ATTACH_KEY,
            "NEW_API_NOTIFY_URL": TEST_NOTIFY_URL,
            "ALIPAY_ENABLED": "false",
            "WECHAT_ENABLED": "false",
            "RATE_LIMIT_PER_MINUTE": "10000",  # disable for most tests
        }
    )


_set_env()


@pytest.fixture(autouse=True)
def _ensure_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env()
    from app.config import reload_settings_for_tests

    reload_settings_for_tests()


# --------- fake providers ---------


@dataclass
class FakeProvider:
    """Test double satisfying the PaymentProvider protocol."""

    redirect_url: str | None = None
    code_url: str | None = None
    prepay_exc: Exception | None = None
    notify_exc: Exception | None = None
    notify_result: Any = None  # NotifyResult
    prepay_calls: list[dict] = field(default_factory=list)
    notify_calls: list[dict] = field(default_factory=list)

    async def prepay(self, *, out_trade_no, amount_fen, subject, attach):
        from app.providers.base import PrepayResult

        self.prepay_calls.append(
            {
                "out_trade_no": out_trade_no,
                "amount_fen": amount_fen,
                "subject": subject,
                "attach": attach,
            }
        )
        if self.prepay_exc:
            raise self.prepay_exc
        return PrepayResult(redirect_url=self.redirect_url, code_url=self.code_url)

    async def parse_notify(self, *, headers, body):
        self.notify_calls.append({"headers": headers, "body": body})
        if self.notify_exc:
            raise self.notify_exc
        return self.notify_result


@pytest.fixture
def fake_alipay() -> FakeProvider:
    return FakeProvider(redirect_url="https://openapi.alipay.com/gateway.do?x=1")


@pytest.fixture
def fake_wechat() -> FakeProvider:
    return FakeProvider(code_url="weixin://wxpay/bizpayurl?pr=AbCdEfG")


@pytest.fixture
def http_mock(monkeypatch: pytest.MonkeyPatch) -> dict[str, list]:
    """Replace AppState.http with a mock that records calls and returns 'success'."""
    calls: list[dict] = []

    class _Resp:
        def __init__(self, status_code: int = 200, text: str = "success") -> None:
            self.status_code = status_code
            self.text = text

    class _MockAsyncClient:
        def __init__(self, default_status=200, default_body="success"):
            self._status = default_status
            self._body = default_body

        async def get(self, url, params=None, timeout=None):
            calls.append({"url": url, "params": dict(params) if params else {}})
            return _Resp(self._status, self._body)

        async def aclose(self):
            pass

    return {"calls": calls, "client_cls": _MockAsyncClient}


@pytest.fixture
def app_with_fakes(fake_alipay, fake_wechat, http_mock):
    """Build a FastAPI app whose providers + http client are mocked."""
    from app.config import get_settings
    from app.deps import AppState
    from app.main import create_app
    from app.payment_method import PaymentMethod

    app = create_app()

    settings = get_settings()
    state = AppState(
        settings=settings,
        http=http_mock["client_cls"](),
        providers={
            PaymentMethod.ALIPAY: fake_alipay,
            PaymentMethod.WXPAY: fake_wechat,
        },
        attach_secret=settings.attach_hmac_key.encode("utf-8"),
        epay_key=settings.epay_key,
    )
    app.state.gateway = state
    # Skip lifespan (it would otherwise try to reload providers).
    app.router.lifespan_context = _noop_lifespan
    return app, state


def _noop_lifespan(app):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _ctx():
        yield

    return _ctx()


@pytest.fixture
def client(app_with_fakes) -> Generator[TestClient, None, None]:
    app, _ = app_with_fakes
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
