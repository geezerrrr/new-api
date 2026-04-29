"""Provider unit tests using SDK doubles (no network)."""

from __future__ import annotations

import pytest

from app.exceptions import ProviderError

# ---------------- Alipay ----------------


class _AlipaySDKStub:
    def __init__(self, *, prepay_string="biz_content=...&sign=ZZZ", verify_ok=True):
        self._prepay_string = prepay_string
        self._verify_ok = verify_ok
        self.last_prepay_kwargs = None
        self.last_verify_args = None

    def api_alipay_trade_page_pay(self, **kwargs):
        self.last_prepay_kwargs = kwargs
        return self._prepay_string

    def verify(self, data, sig):
        self.last_verify_args = (data, sig)
        return self._verify_ok


@pytest.mark.asyncio
async def test_alipay_prepay_builds_full_redirect_url():
    from app.providers.alipay import AlipayProvider

    sdk = _AlipaySDKStub()
    p = AlipayProvider(
        sdk,
        gateway_url="https://openapi.alipay.com/gateway.do",
        notify_url="https://pay.example.com/alipay/notify",
        return_url="https://pay.example.com/alipay/return",
    )
    r = await p.prepay(
        out_trade_no="USR1NOABC", amount_fen=1000,
        subject="TUC100", attach="ABC",
    )
    assert r.redirect_url and r.redirect_url.startswith(
        "https://openapi.alipay.com/gateway.do?"
    )
    # Amount converted from fen to yuan.
    assert sdk.last_prepay_kwargs["total_amount"] == "10.00"
    assert sdk.last_prepay_kwargs["passback_params"] == "ABC"


def _alipay_body(notify_time: str | None = None) -> bytes:
    """Build a fresh-by-default Alipay notify body."""
    if notify_time is None:
        from datetime import datetime, timedelta, timezone

        beijing = datetime.now(timezone(timedelta(hours=8)))
        notify_time = beijing.strftime("%Y-%m-%d %H:%M:%S")
    return (
        f"out_trade_no=USR1NOABC"
        f"&trade_no=alipay-tx"
        f"&trade_status=TRADE_SUCCESS"
        f"&total_amount=10.00"
        f"&passback_params=MY_ATTACH"
        f"&notify_time={notify_time.replace(' ', '+')}"
        f"&sign=fake&sign_type=RSA2"
    ).encode()


@pytest.mark.asyncio
async def test_alipay_parse_notify_success():
    from app.providers.alipay import AlipayProvider

    sdk = _AlipaySDKStub(verify_ok=True)
    p = AlipayProvider(sdk, gateway_url="x", notify_url="y", return_url="z")
    r = await p.parse_notify(headers={}, body=_alipay_body())
    assert r.out_trade_no == "USR1NOABC"
    assert r.amount_fen == 1000
    assert r.attach == "MY_ATTACH"
    assert r.payment_method == "alipay"


@pytest.mark.asyncio
async def test_alipay_parse_notify_rejects_stale_replay():
    """A captured-and-replayed notify (older than the window) is rejected
    even though its RSA signature would otherwise verify."""
    from app.providers.alipay import AlipayProvider

    sdk = _AlipaySDKStub(verify_ok=True)
    p = AlipayProvider(sdk, gateway_url="x", notify_url="y", return_url="z")
    with pytest.raises(ProviderError, match="out of window"):
        # 2 hours old > 10 min window
        await p.parse_notify(headers={}, body=_alipay_body("2020-01-01 00:00:00"))


@pytest.mark.asyncio
async def test_alipay_parse_notify_rejects_missing_notify_time():
    from app.providers.alipay import AlipayProvider

    sdk = _AlipaySDKStub(verify_ok=True)
    p = AlipayProvider(sdk, gateway_url="x", notify_url="y", return_url="z")
    body = (
        b"out_trade_no=USR1NOABC&trade_status=TRADE_SUCCESS"
        b"&total_amount=10.00&sign=fake&sign_type=RSA2"
    )
    with pytest.raises(ProviderError, match="notify_time missing"):
        await p.parse_notify(headers={}, body=body)


@pytest.mark.asyncio
async def test_alipay_parse_notify_rejects_malformed_notify_time():
    from app.providers.alipay import AlipayProvider

    sdk = _AlipaySDKStub(verify_ok=True)
    p = AlipayProvider(sdk, gateway_url="x", notify_url="y", return_url="z")
    body = _alipay_body().replace(b"notify_time=", b"notify_time=garbage").replace(
        b"+", b"%20"
    )
    with pytest.raises(ProviderError):
        await p.parse_notify(headers={}, body=body)


@pytest.mark.asyncio
async def test_alipay_parse_notify_rejects_bad_signature():
    from app.providers.alipay import AlipayProvider

    sdk = _AlipaySDKStub(verify_ok=False)
    p = AlipayProvider(sdk, gateway_url="x", notify_url="y", return_url="z")
    with pytest.raises(ProviderError):
        await p.parse_notify(headers={}, body=b"trade_status=TRADE_SUCCESS&sign=x")


@pytest.mark.asyncio
async def test_alipay_parse_notify_rejects_non_success_status():
    from app.providers.alipay import AlipayProvider

    sdk = _AlipaySDKStub(verify_ok=True)
    p = AlipayProvider(sdk, gateway_url="x", notify_url="y", return_url="z")
    with pytest.raises(ProviderError):
        await p.parse_notify(
            headers={},
            body=b"out_trade_no=X&trade_status=WAIT_BUYER_PAY&total_amount=1.00&sign=x",
        )


@pytest.mark.asyncio
async def test_alipay_parse_notify_rejects_missing_total_amount():
    from app.providers.alipay import AlipayProvider

    sdk = _AlipaySDKStub(verify_ok=True)
    p = AlipayProvider(sdk, gateway_url="x", notify_url="y", return_url="z")
    with pytest.raises(ProviderError):
        await p.parse_notify(
            headers={},
            body=b"out_trade_no=X&trade_status=TRADE_SUCCESS&sign=x",
        )


@pytest.mark.asyncio
async def test_alipay_parse_notify_rejects_non_utf8_body():
    from app.providers.alipay import AlipayProvider

    sdk = _AlipaySDKStub(verify_ok=True)
    p = AlipayProvider(sdk, gateway_url="x", notify_url="y", return_url="z")
    with pytest.raises(ProviderError):
        await p.parse_notify(headers={}, body=b"\xff\xfe-broken")


# ---------------- WeChat ----------------


class _WechatSDKStub:
    def __init__(self, *, pay_response=(200, '{"code_url":"weixin://wxpay/bizpayurl?pr=ABC"}'),
                 callback_response=None, callback_raises=False):
        self._pay_response = pay_response
        self._callback_response = callback_response
        self._callback_raises = callback_raises
        self.last_pay_kwargs = None

    def pay(self, **kwargs):
        self.last_pay_kwargs = kwargs
        return self._pay_response

    def callback(self, headers, body):
        if self._callback_raises:
            raise RuntimeError("verify error")
        return self._callback_response


@pytest.mark.asyncio
async def test_wechat_prepay_returns_code_url():
    from app.providers.wechat import WechatProvider

    sdk = _WechatSDKStub()
    p = WechatProvider(sdk, notify_url="https://pay.example.com/wechat/notify")
    r = await p.prepay(out_trade_no="USR1NOABC", amount_fen=1000,
                       subject="TUC100", attach="A")
    assert r.code_url and r.code_url.startswith("weixin://")
    assert sdk.last_pay_kwargs["amount"] == {"total": 1000, "currency": "CNY"}


@pytest.mark.asyncio
async def test_wechat_prepay_raises_when_response_missing_code_url():
    from app.providers.wechat import WechatProvider

    sdk = _WechatSDKStub(pay_response=(200, "{}"))
    p = WechatProvider(sdk, notify_url="x")
    with pytest.raises(ProviderError):
        await p.prepay(out_trade_no="X", amount_fen=100, subject="x", attach="A")


@pytest.mark.asyncio
async def test_wechat_prepay_raises_on_non_200():
    from app.providers.wechat import WechatProvider

    sdk = _WechatSDKStub(pay_response=(500, '{"code":"SYSTEM_ERROR"}'))
    p = WechatProvider(sdk, notify_url="x")
    with pytest.raises(ProviderError):
        await p.prepay(out_trade_no="X", amount_fen=100, subject="x", attach="A")


def _wechat_headers(timestamp: int | None = None) -> dict[str, str]:
    import time as _t

    if timestamp is None:
        timestamp = int(_t.time())
    return {"wechatpay-timestamp": str(timestamp)}


@pytest.mark.asyncio
async def test_wechat_parse_notify_success():
    from app.providers.wechat import WechatProvider

    payload = {
        "event_type": "TRANSACTION.SUCCESS",
        "resource": {
            "out_trade_no": "USR1NOABC",
            "transaction_id": "wx-tx-001",
            "trade_state": "SUCCESS",
            "amount": {"total": 1000, "currency": "CNY"},
            "attach": "MY_ATTACH",
        },
    }
    sdk = _WechatSDKStub(callback_response=payload)
    p = WechatProvider(sdk, notify_url="x")
    r = await p.parse_notify(headers=_wechat_headers(), body=b"{}")
    assert r.out_trade_no == "USR1NOABC"
    assert r.amount_fen == 1000
    assert r.attach == "MY_ATTACH"
    assert r.payment_method == "wxpay"


@pytest.mark.asyncio
async def test_wechat_parse_notify_rejects_stale_replay():
    """SDK RSA-verifies but does NOT check timestamp staleness — we add this
    defense-in-depth ourselves."""
    from app.providers.wechat import WechatProvider

    sdk = _WechatSDKStub(callback_response={"event_type": "TRANSACTION.SUCCESS"})
    p = WechatProvider(sdk, notify_url="x")
    # 1 hour ago, > 5 min window
    stale_ts = int(__import__("time").time()) - 3600
    with pytest.raises(ProviderError, match="out of window"):
        await p.parse_notify(headers=_wechat_headers(stale_ts), body=b"{}")


@pytest.mark.asyncio
async def test_wechat_parse_notify_rejects_missing_timestamp():
    from app.providers.wechat import WechatProvider

    sdk = _WechatSDKStub(callback_response={"event_type": "TRANSACTION.SUCCESS"})
    p = WechatProvider(sdk, notify_url="x")
    with pytest.raises(ProviderError, match="missing/invalid timestamp"):
        await p.parse_notify(headers={}, body=b"{}")


@pytest.mark.asyncio
async def test_wechat_parse_notify_rejects_non_success_event():
    from app.providers.wechat import WechatProvider

    sdk = _WechatSDKStub(callback_response={"event_type": "TRANSACTION.PAY.FAIL"})
    p = WechatProvider(sdk, notify_url="x")
    with pytest.raises(ProviderError):
        await p.parse_notify(headers=_wechat_headers(), body=b"{}")


@pytest.mark.asyncio
async def test_wechat_parse_notify_rejects_when_callback_raises():
    from app.providers.wechat import WechatProvider

    sdk = _WechatSDKStub(callback_raises=True)
    p = WechatProvider(sdk, notify_url="x")
    with pytest.raises(ProviderError):
        await p.parse_notify(headers=_wechat_headers(), body=b"{}")


@pytest.mark.asyncio
async def test_wechat_parse_notify_rejects_when_callback_returns_none():
    from app.providers.wechat import WechatProvider

    sdk = _WechatSDKStub(callback_response=None)
    p = WechatProvider(sdk, notify_url="x")
    with pytest.raises(ProviderError):
        await p.parse_notify(headers=_wechat_headers(), body=b"{}")
