from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas import EpaySubmitParams, money_to_fen


def _ok_params(**overrides):
    base = {
        "pid": "1000",
        "type": "alipay",
        "out_trade_no": "USR1NOABC",
        "notify_url": "https://api.example.com/api/user/epay/notify",
        "return_url": "https://app.example.com/console/log",
        "name": "TUC100",
        "money": "10.00",
        "device": "pc",
        "sign": "0" * 32,
        "sign_type": "MD5",
    }
    base.update(overrides)
    return base


def test_accepts_alipay_and_wxpay():
    EpaySubmitParams(**_ok_params(type="alipay"))
    EpaySubmitParams(**_ok_params(type="wxpay"))


def test_rejects_unknown_payment_type():
    with pytest.raises(ValidationError):
        EpaySubmitParams(**_ok_params(type="bitcoin"))


def test_money_normalised_to_two_decimals():
    p = EpaySubmitParams(**_ok_params(money="10"))
    # Schema canonicalises to two decimals so signing is deterministic.
    assert p.money == "10.00"


def test_money_rejects_too_many_decimals():
    with pytest.raises(ValidationError):
        EpaySubmitParams(**_ok_params(money="10.001"))


def test_money_rejects_zero():
    with pytest.raises(ValidationError):
        EpaySubmitParams(**_ok_params(money="0.00"))


def test_money_rejects_negative():
    with pytest.raises(ValidationError):
        EpaySubmitParams(**_ok_params(money="-1.00"))


def test_money_rejects_huge():
    with pytest.raises(ValidationError):
        EpaySubmitParams(**_ok_params(money="9999999.00"))


def test_money_rejects_scientific():
    with pytest.raises(ValidationError):
        EpaySubmitParams(**_ok_params(money="1e2"))


def test_money_rejects_garbage():
    with pytest.raises(ValidationError):
        EpaySubmitParams(**_ok_params(money="abc"))


def test_money_rejects_unicode_digits():
    """Defense in depth: Decimal() silently accepts Arabic-Indic / Devanagari
    digits (e.g., Decimal('١٠') == 10), and Python's default `\\d` matches
    them too. The regex must use re.ASCII to reject non-ASCII digits at the
    boundary, before any ambiguity can leak through."""
    with pytest.raises(ValidationError):
        EpaySubmitParams(**_ok_params(money="١٠"))  # Arabic-Indic 10
    with pytest.raises(ValidationError):
        EpaySubmitParams(**_ok_params(money="१०"))  # Devanagari 10


def test_out_trade_no_rejects_special_chars():
    with pytest.raises(ValidationError):
        EpaySubmitParams(**_ok_params(out_trade_no="abc/../etc/passwd"))
    with pytest.raises(ValidationError):
        EpaySubmitParams(**_ok_params(out_trade_no="abc;DROP TABLE"))
    with pytest.raises(ValidationError):
        EpaySubmitParams(**_ok_params(out_trade_no="abc def"))


def test_sign_must_be_md5_hex():
    with pytest.raises(ValidationError):
        EpaySubmitParams(**_ok_params(sign="0" * 31))  # too short
    with pytest.raises(ValidationError):
        EpaySubmitParams(**_ok_params(sign="g" * 32))  # invalid hex


def test_money_to_fen():
    assert money_to_fen("10.00") == 1000
    assert money_to_fen("0.01") == 1
    assert money_to_fen("999999.99") == 99999999


def test_money_to_fen_rejects_zero():
    with pytest.raises(ValueError):
        money_to_fen("0")
