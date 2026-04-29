"""Pydantic models for inbound requests + amount conversion helpers.

This is the only place where untrusted strings turn into typed values.
Everything below this layer can assume:
  - `money` is a canonical NN.DD decimal in the range (0, 1_000_000)
  - `out_trade_no` matches `[A-Za-z0-9_-]+`
  - `type` is a known PaymentMethod
  - `sign` is a 32-char lowercase hex digest
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from .payment_method import PaymentMethod

# `re.ASCII` is critical here: without it, `\d` matches Unicode digit
# categories (Arabic-Indic ١٠, Devanagari १०, etc.) AND `Decimal()` happens
# to accept those, so `money="١٠"` would pass both layers undetected and
# silently become "10.00" on canonicalize. We want strict NN[.DD] ASCII.
_MONEY_RE = re.compile(r"^\d+(\.\d{1,2})?$", re.ASCII)


class EpaySubmitParams(BaseModel):
    """Parameters posted to /submit.php by new-api (epay protocol)."""

    pid: str = Field(min_length=1, max_length=64)
    type: PaymentMethod
    out_trade_no: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_\-]+$")
    notify_url: str = Field(min_length=1, max_length=512)
    return_url: str = Field(default="", max_length=512)
    name: str = Field(min_length=1, max_length=128)
    money: str = Field(min_length=1, max_length=16)
    device: str = Field(default="pc", max_length=16)
    sign: str = Field(min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$")
    sign_type: Literal["MD5"] = "MD5"

    @field_validator("money")
    @classmethod
    def _money_is_valid_decimal(cls, v: str) -> str:
        # Strict regex first — reject scientific notation, signs, whitespace,
        # >2 fraction digits BEFORE Decimal() gets to lenient-parse them.
        if not _MONEY_RE.match(v):
            raise ValueError("money must be NNN or NNN.DD format")
        d = Decimal(v)
        if d <= 0 or d >= Decimal("1000000"):
            raise ValueError("money out of range")
        # Re-canonicalize so signing is deterministic.
        return format(d.quantize(Decimal("0.01")), "f")


# ---------------- helpers ----------------

def money_to_fen(money_yuan: str) -> int:
    """Convert decimal yuan string to int fen. Defensive even though `money`
    is already validated — never compute amounts in float."""
    d = Decimal(money_yuan).quantize(Decimal("0.01"))
    fen = int((d * 100).to_integral_value())
    if fen <= 0:
        raise ValueError("non-positive amount")
    return fen
