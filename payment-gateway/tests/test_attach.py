from __future__ import annotations

import pytest

from app.attach import make_attach, open_attach
from app.exceptions import InvalidAttach

SECRET = b"super-secret-key-32-bytes-min!!"


def test_round_trip():
    token = make_attach(out_trade_no="USR1NOABC", money="10.00", secret=SECRET)
    assert isinstance(token, str)
    assert "=" not in token  # base64url stripped padding
    no, money = open_attach(token, secret=SECRET)
    assert no == "USR1NOABC"
    assert money == "10.00"


def test_token_is_url_safe():
    # Many round-trips — stress that we never emit + or /
    for i in range(1000):
        t = make_attach(
            out_trade_no=f"O{i:08d}",
            money=f"{(i % 999) + 0.01:.2f}",
            secret=SECRET,
        )
        assert all(c.isalnum() or c in "-_" for c in t)


def test_token_size_within_wechat_attach_limit():
    # WeChat caps `attach` at 128 chars. Our tokens must fit.
    t = make_attach(
        out_trade_no="USR9999999NO" + "X" * 30,  # realistic upper bound
        money="999999.99",
        secret=SECRET,
    )
    assert len(t) <= 128


def test_open_rejects_garbage():
    with pytest.raises(InvalidAttach):
        open_attach("not-a-real-token!!!", secret=SECRET)


def test_open_rejects_empty():
    with pytest.raises(InvalidAttach):
        open_attach("", secret=SECRET)


def test_open_rejects_when_token_too_long():
    with pytest.raises(InvalidAttach):
        open_attach("A" * 1024, secret=SECRET)


def test_open_rejects_truncated_token():
    t = make_attach(out_trade_no="X", money="1.00", secret=SECRET)
    with pytest.raises(InvalidAttach):
        open_attach(t[:8], secret=SECRET)


def test_open_rejects_wrong_secret():
    t = make_attach(out_trade_no="X", money="1.00", secret=SECRET)
    with pytest.raises(InvalidAttach):
        open_attach(t, secret=b"different-secret-of-correct-length")


def test_open_detects_payload_tamper():
    # Flip one byte in the payload section (after the 16-byte HMAC tag).
    import base64

    t = make_attach(out_trade_no="X", money="1.00", secret=SECRET)
    raw = bytearray(base64.urlsafe_b64decode(t + "=" * (-len(t) % 4)))
    raw[-1] ^= 1
    forged = (
        base64.urlsafe_b64encode(bytes(raw)).rstrip(b"=").decode("ascii")
    )
    with pytest.raises(InvalidAttach):
        open_attach(forged, secret=SECRET)


def test_open_detects_hmac_tag_tamper():
    import base64

    t = make_attach(out_trade_no="X", money="1.00", secret=SECRET)
    raw = bytearray(base64.urlsafe_b64decode(t + "=" * (-len(t) % 4)))
    raw[0] ^= 1
    forged = (
        base64.urlsafe_b64encode(bytes(raw)).rstrip(b"=").decode("ascii")
    )
    with pytest.raises(InvalidAttach):
        open_attach(forged, secret=SECRET)


def test_make_rejects_separator_in_field():
    with pytest.raises(ValueError):
        make_attach(out_trade_no="abc|def", money="1.00", secret=SECRET)
    with pytest.raises(ValueError):
        make_attach(out_trade_no="abc", money="1|00", secret=SECRET)


def test_open_rejects_non_utf8_payload():
    # Construct a valid-MAC token whose payload bytes are invalid UTF-8.
    import base64
    import hmac
    from hashlib import sha256

    payload = b"\xff\xfe\xfdbroken"
    tag = hmac.new(SECRET, payload, sha256).digest()[:16]
    forged = base64.urlsafe_b64encode(tag + payload).rstrip(b"=").decode()
    with pytest.raises(InvalidAttach):
        open_attach(forged, secret=SECRET)


def test_open_rejects_trailing_junk():
    """`base64.urlsafe_b64decode` silently extends the byte stream when given
    extra valid base64 chars at the end. The HMAC must catch this — the
    appended bytes become part of the verified payload, breaking the MAC."""
    import base64

    valid = make_attach(out_trade_no="X", money="1.00", secret=SECRET)
    # Append more valid base64 chars (4 chars = 3 bytes) that decode cleanly
    # but make the resulting payload longer than what we MAC'd.
    tampered = valid + "zzzz"
    with pytest.raises(InvalidAttach):
        open_attach(tampered, secret=SECRET)
    # Sanity: the stdlib really does silently decode the extension.
    raw = base64.urlsafe_b64decode(valid + "zzzz" + "=" * (-(len(valid) + 4) % 4))
    assert len(raw) > 16  # tag(16) + original payload(~6) + extra(3)


def test_open_rejects_payload_without_separator():
    import base64
    import hmac
    from hashlib import sha256

    payload = b"no-separator"
    tag = hmac.new(SECRET, payload, sha256).digest()[:16]
    forged = base64.urlsafe_b64encode(tag + payload).rstrip(b"=").decode()
    with pytest.raises(InvalidAttach):
        open_attach(forged, secret=SECRET)
