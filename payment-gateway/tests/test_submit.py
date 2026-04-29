"""End-to-end tests for /submit.php using fake providers."""

from __future__ import annotations

from app.epay import sign
from tests.conftest import TEST_EPAY_KEY, TEST_EPAY_PID


def _signed(extra: dict | None = None, *, type_="alipay"):
    p = {
        "pid": TEST_EPAY_PID,
        "type": type_,
        "out_trade_no": "USR1NOABC",
        "notify_url": "https://api.example.com/api/user/epay/notify",
        "return_url": "https://app.example.com/console/log",
        "name": "TUC100",
        "money": "10.00",
        "device": "pc",
        "sign_type": "MD5",
    }
    if extra:
        p.update(extra)
    p["sign"] = sign(p, TEST_EPAY_KEY)
    return p


def test_submit_alipay_post_redirects(client, fake_alipay):
    r = client.post("/submit.php", data=_signed(), follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"].startswith("https://openapi.alipay.com/")
    # Provider was called with the right args.
    call = fake_alipay.prepay_calls[0]
    assert call["amount_fen"] == 1000
    assert call["out_trade_no"] == "USR1NOABC"
    assert call["subject"] == "TUC100"
    assert call["attach"]  # opaque, but non-empty


def test_submit_alipay_get_works(client):
    r = client.get("/submit.php", params=_signed(), follow_redirects=False)
    assert r.status_code == 302


def test_submit_wechat_renders_qrcode_html(client, fake_wechat):
    r = client.post("/submit.php", data=_signed(type_="wxpay"))
    assert r.status_code == 200
    assert "image/png;base64" in r.text
    assert "USR1NOABC" in r.text  # rendered into the page (escaped)
    assert fake_wechat.prepay_calls


def test_submit_rejects_bad_signature(client):
    p = _signed()
    p["sign"] = "0" * 32
    r = client.post("/submit.php", data=p, follow_redirects=False)
    assert r.status_code == 400
    assert b"invalid signature" in r.content


def test_submit_rejects_missing_signature(client):
    p = _signed()
    del p["sign"]
    r = client.post("/submit.php", data=p, follow_redirects=False)
    assert r.status_code == 400


def test_submit_rejects_pid_mismatch_after_sign(client):
    # Forge a sig with a different pid that happens to verify (using OUR key);
    # PID cross-check is defense-in-depth.
    p = _signed({"pid": "9999"})
    r = client.post("/submit.php", data=p, follow_redirects=False)
    assert r.status_code == 400


def test_submit_rejects_unsupported_type(client):
    # Invalid `type` is rejected by the schema; signature still computed.
    p = _signed({"type": "bitcoin"})
    p["sign"] = sign({k: v for k, v in p.items() if k != "sign"}, TEST_EPAY_KEY)
    r = client.post("/submit.php", data=p, follow_redirects=False)
    # Pydantic catches this as 400 (invalid params).
    assert r.status_code == 400


def test_submit_rejects_money_with_too_many_decimals(client):
    p = _signed({"money": "10.001"})
    r = client.post("/submit.php", data=p, follow_redirects=False)
    assert r.status_code == 400


def test_submit_rejects_when_provider_disabled(client, app_with_fakes):
    _, state = app_with_fakes
    from app.payment_method import PaymentMethod

    state.providers.pop(PaymentMethod.ALIPAY, None)  # disable
    r = client.post("/submit.php", data=_signed(), follow_redirects=False)
    assert r.status_code == 400


def test_submit_returns_400_when_provider_raises(client, fake_alipay):
    from app.exceptions import ProviderError

    fake_alipay.prepay_exc = ProviderError("upstream broken")
    r = client.post("/submit.php", data=_signed(), follow_redirects=False)
    assert r.status_code == 400


def test_submit_sets_security_headers(client):
    r = client.post("/submit.php", data=_signed(), follow_redirects=False)
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert "strict-transport-security" in r.headers
