"""epay sign/verify + outbound notify."""

from __future__ import annotations

import hashlib

import httpx
import pytest

from app.epay import (
    TRADE_SUCCESS,
    _canonical_string,
    build_notify_params,
    deliver_notify,
    sign,
    verify,
)
from app.exceptions import InvalidEpaySignature, UpstreamNotifyFailed
from app.payment_method import PaymentMethod

KEY = "test_merchant_key"


# ---------------- algorithm correctness ----------------


def test_canonical_string_filters_sign_and_empty():
    p = {
        "pid": "1000",
        "type": "alipay",
        "money": "10.00",
        "out_trade_no": "T1",
        "sign": "should-be-stripped",
        "sign_type": "MD5",
        "empty": "",
    }
    assert _canonical_string(p) == "money=10.00&out_trade_no=T1&pid=1000&type=alipay"


def test_canonical_string_lex_order_not_natural_order():
    # Lexicographic sort: "10" < "9" — confirm we follow string sort, like go-epay.
    p = {"a10": "x", "a9": "y", "a2": "z"}
    assert _canonical_string(p) == "a10=x&a2=z&a9=y"


def test_sign_matches_protocol():
    p = {"pid": "1000", "type": "alipay", "money": "10.00", "out_trade_no": "ABC123"}
    expected_canonical = "money=10.00&out_trade_no=ABC123&pid=1000&type=alipay"
    expected_md5 = hashlib.md5((expected_canonical + KEY).encode()).hexdigest()
    assert sign(p, KEY) == expected_md5


def test_sign_lowercase_hex():
    p = {"a": "b"}
    s = sign(p, KEY)
    assert s == s.lower() and len(s) == 32


def test_sign_unicode_safe():
    p = {"name": "充值订单", "money": "1.00"}
    # Just must not raise; result is deterministic.
    assert sign(p, KEY) == sign(p, KEY)


def test_verify_accepts_correct_signature():
    p = {"pid": "1000", "type": "alipay", "money": "10.00"}
    p["sign"] = sign(p, KEY)
    p["sign_type"] = "MD5"
    verify(p, KEY)  # no raise


def test_verify_rejects_missing_sign():
    p = {"pid": "1000", "type": "alipay", "money": "10.00"}
    with pytest.raises(InvalidEpaySignature):
        verify(p, KEY)


def test_verify_rejects_wrong_signature():
    p = {"pid": "1000", "type": "alipay", "money": "10.00", "sign": "0" * 32}
    with pytest.raises(InvalidEpaySignature):
        verify(p, KEY)


def test_verify_rejects_when_field_tampered():
    p = {"pid": "1000", "type": "alipay", "money": "10.00"}
    p["sign"] = sign(p, KEY)
    p["money"] = "0.01"  # silently changed
    with pytest.raises(InvalidEpaySignature):
        verify(p, KEY)


def test_verify_uppercase_sign_normalised_then_compared():
    p = {"pid": "1000", "type": "alipay", "money": "10.00"}
    p["sign"] = sign(p, KEY).upper()
    verify(p, KEY)  # tolerated (case-insensitive on inbound)


def test_sign_excludes_sign_field_even_if_present():
    p1 = {"pid": "1000", "money": "10.00"}
    p2 = {"pid": "1000", "money": "10.00", "sign": "anything", "sign_type": "MD5"}
    assert sign(p1, KEY) == sign(p2, KEY)


# ---------------- build_notify_params ----------------


def test_build_notify_params_signs_with_correct_key():
    p = build_notify_params(
        pid="1000",
        epay_key=KEY,
        trade_no="alipay_tx_001",
        out_trade_no="USR1NOABC",
        payment_type=PaymentMethod.ALIPAY,
        name="topup",
        money="10.00",
    )
    # Verify with the same key.
    verify(p, KEY)
    assert p["trade_status"] == TRADE_SUCCESS
    assert p["sign_type"] == "MD5"


def test_build_notify_params_changes_with_key():
    p1 = build_notify_params(
        pid="1000", epay_key="k1", trade_no="t", out_trade_no="o",
        payment_type=PaymentMethod.ALIPAY, name="n", money="1.00",
    )
    p2 = build_notify_params(
        pid="1000", epay_key="k2", trade_no="t", out_trade_no="o",
        payment_type=PaymentMethod.ALIPAY, name="n", money="1.00",
    )
    assert p1["sign"] != p2["sign"]


# ---------------- deliver_notify ----------------


@pytest.mark.asyncio
async def test_deliver_notify_ok():
    def handler(request):
        assert request.url.params.get("trade_status") == TRADE_SUCCESS
        return httpx.Response(200, text="success")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as c:
        await deliver_notify(c, "https://api.example.com/notify",
                             {"trade_status": TRADE_SUCCESS}, timeout=1.0)


@pytest.mark.asyncio
async def test_deliver_notify_raises_when_body_not_success():
    transport = httpx.MockTransport(lambda r: httpx.Response(200, text="fail"))
    async with httpx.AsyncClient(transport=transport) as c:
        with pytest.raises(UpstreamNotifyFailed):
            await deliver_notify(c, "https://api.example.com/notify", {}, timeout=1.0)


@pytest.mark.asyncio
async def test_deliver_notify_raises_when_status_not_200():
    transport = httpx.MockTransport(lambda r: httpx.Response(500, text="success"))
    async with httpx.AsyncClient(transport=transport) as c:
        with pytest.raises(UpstreamNotifyFailed):
            await deliver_notify(c, "https://api.example.com/notify", {}, timeout=1.0)


@pytest.mark.asyncio
async def test_deliver_notify_raises_on_network_error():
    def boom(r): raise httpx.ConnectError("nope")
    transport = httpx.MockTransport(boom)
    async with httpx.AsyncClient(transport=transport) as c:
        with pytest.raises(UpstreamNotifyFailed):
            await deliver_notify(c, "https://api.example.com/notify", {}, timeout=1.0)
