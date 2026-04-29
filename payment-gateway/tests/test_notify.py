"""/alipay/notify and /wechat/notify integration tests with fake providers."""

from __future__ import annotations

from app.attach import make_attach
from app.exceptions import ProviderError
from app.payment_method import PaymentMethod
from app.providers.base import NotifyResult


def _make_result(
    *,
    attach: str,
    amount_fen: int = 1000,
    payment_method: PaymentMethod = PaymentMethod.ALIPAY,
    out_trade_no: str = "USR1NOABC",
):
    return NotifyResult(
        out_trade_no=out_trade_no,
        provider_trade_no="upstream-tx-001",
        amount_fen=amount_fen,
        attach=attach,
        payment_method=payment_method,
    )


# ---------------- alipay ----------------


def test_alipay_notify_happy_path(client, fake_alipay, app_with_fakes, http_mock):
    _, state = app_with_fakes
    attach = make_attach(out_trade_no="USR1NOABC", money="10.00",
                         secret=state.attach_secret)
    fake_alipay.notify_result = _make_result(attach=attach)
    r = client.post("/alipay/notify", content=b"trade_status=TRADE_SUCCESS&...")
    assert r.status_code == 200
    assert r.text == "success"
    # The forwarded notify hit the right endpoint with epay-format params.
    assert len(http_mock["calls"]) == 1
    fwd = http_mock["calls"][0]
    assert fwd["url"].endswith("/api/user/epay/notify")
    assert fwd["params"]["out_trade_no"] == "USR1NOABC"
    assert fwd["params"]["money"] == "10.00"
    assert fwd["params"]["type"] == "alipay"
    assert fwd["params"]["trade_status"] == "TRADE_SUCCESS"
    assert "sign" in fwd["params"]


def test_alipay_notify_returns_fail_when_provider_verify_fails(client, fake_alipay):
    fake_alipay.notify_exc = ProviderError("bad sign")
    r = client.post("/alipay/notify", content=b"junk")
    assert r.status_code == 200
    assert r.text == "fail"


def test_alipay_notify_returns_fail_on_attach_tamper(client, fake_alipay, app_with_fakes):
    _, state = app_with_fakes
    bad_attach = make_attach(out_trade_no="USR1NOABC", money="10.00",
                             secret=b"WRONG_SECRET_OF_PROPER_LENGTH!")
    fake_alipay.notify_result = _make_result(attach=bad_attach)
    r = client.post("/alipay/notify", content=b"x")
    assert r.text == "fail"


def test_alipay_notify_returns_fail_on_amount_tamper(client, fake_alipay,
                                                     app_with_fakes, http_mock):
    """Amount mismatch is a security event: we return Alipay's protocol-level
    'fail' (HTTP 200 'fail' body) and never forward to new-api."""
    _, state = app_with_fakes
    # Attach pinned to 10.00 but the SDK reports 9999.00 fen.
    attach = make_attach(out_trade_no="USR1NOABC", money="10.00",
                         secret=state.attach_secret)
    fake_alipay.notify_result = _make_result(attach=attach, amount_fen=999900)
    r = client.post("/alipay/notify", content=b"x")
    assert r.status_code == 200
    assert r.text == "fail"
    # No forwarding to new-api on a security failure.
    assert http_mock["calls"] == []


def test_alipay_notify_returns_fail_when_trade_no_mismatch(
    client, fake_alipay, app_with_fakes, http_mock,
):
    _, state = app_with_fakes
    attach = make_attach(out_trade_no="DIFFERENT", money="10.00",
                         secret=state.attach_secret)
    fake_alipay.notify_result = _make_result(attach=attach)  # callback says USR1NOABC
    r = client.post("/alipay/notify", content=b"x")
    assert r.text == "fail"
    assert http_mock["calls"] == []


def test_alipay_notify_returns_fail_when_upstream_fails(
    client, fake_alipay, app_with_fakes, http_mock,
):
    """Critical: if new-api doesn't ack, we MUST NOT ack the platform —
    Alipay/WeChat will retry, which is our durability mechanism."""
    _, state = app_with_fakes
    attach = make_attach(out_trade_no="USR1NOABC", money="10.00",
                         secret=state.attach_secret)
    fake_alipay.notify_result = _make_result(attach=attach)

    # Replace http with one that returns "fail".
    state.http = http_mock["client_cls"](default_status=200, default_body="fail")
    r = client.post("/alipay/notify", content=b"x")
    assert r.text == "fail"


def test_alipay_notify_returns_fail_when_provider_returns_wrong_method(
    client, fake_alipay, app_with_fakes, http_mock
):
    """Defensive: if a provider mis-tags its own method, the cross-check
    rejects (no forward, no credit)."""
    from app.payment_method import PaymentMethod

    _, state = app_with_fakes
    attach = make_attach(out_trade_no="USR1NOABC", money="10.00",
                         secret=state.attach_secret)
    # alipay route, but the provider returns wxpay — registry mis-wiring.
    fake_alipay.notify_result = _make_result(
        attach=attach, payment_method=PaymentMethod.WXPAY
    )
    r = client.post("/alipay/notify", content=b"x")
    assert r.text == "fail"
    assert http_mock["calls"] == []


def test_alipay_notify_returns_fail_when_disabled(client, app_with_fakes):
    _, state = app_with_fakes
    from app.payment_method import PaymentMethod

    state.providers.pop(PaymentMethod.ALIPAY, None)
    r = client.post("/alipay/notify", content=b"x")
    assert r.text == "fail"


# ---------------- wechat ----------------


def test_wechat_notify_happy_path(client, fake_wechat, app_with_fakes, http_mock):
    _, state = app_with_fakes
    attach = make_attach(out_trade_no="USR1NOABC", money="10.00",
                         secret=state.attach_secret)
    fake_wechat.notify_result = _make_result(attach=attach, payment_method=PaymentMethod.WXPAY)
    r = client.post("/wechat/notify", content=b'{"resource":{}}')
    assert r.status_code == 200
    assert r.json()["code"] == "SUCCESS"
    assert http_mock["calls"][0]["params"]["type"] == "wxpay"


def test_wechat_notify_returns_5xx_when_upstream_fails(
    client, fake_wechat, app_with_fakes, http_mock,
):
    _, state = app_with_fakes
    attach = make_attach(out_trade_no="USR1NOABC", money="10.00",
                         secret=state.attach_secret)
    fake_wechat.notify_result = _make_result(attach=attach, payment_method=PaymentMethod.WXPAY)
    state.http = http_mock["client_cls"](default_status=500, default_body="boom")
    r = client.post("/wechat/notify", content=b'{}')
    # 5xx tells WeChat to retry.
    assert r.status_code == 500
    assert r.json()["code"] == "FAIL"


def test_wechat_notify_returns_5xx_when_provider_verify_fails(client, fake_wechat):
    fake_wechat.notify_exc = ProviderError("bad sign")
    r = client.post("/wechat/notify", content=b"forged")
    assert r.status_code == 500
