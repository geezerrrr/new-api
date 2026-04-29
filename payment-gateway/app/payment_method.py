"""Single source of truth for the set of payment methods we accept.

Using a `StrEnum` (Python 3.11+) keeps members as plain strings — pydantic,
dict lookups, JSON serialization, and dataclass fields all transparently
accept enum values, so the type-safety upgrade is invisible at call sites.

When adding a new payment method, you only edit this file and the provider
module. Routing, schemas, and state look up via the enum."""

from __future__ import annotations

from enum import StrEnum


class PaymentMethod(StrEnum):
    # The values must match the `type=` parameter passed by new-api in the
    # epay submit (which in turn matches operation_setting/payment_setting_old.go).
    ALIPAY = "alipay"
    WXPAY = "wxpay"
