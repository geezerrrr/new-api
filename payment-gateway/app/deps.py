"""Application-state container.

Built once in the lifespan and stashed on `app.state.gateway`. Routes read
it via `request.app.state.gateway`. We deliberately avoid FastAPI's
`Depends` system here because (a) every route needs the same state and
(b) we want zero overhead on the hot path."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx

from .payment_method import PaymentMethod

if TYPE_CHECKING:
    from .config import Settings
    from .providers.base import PaymentProvider


@dataclass(slots=True)
class AppState:
    settings: Settings
    http: httpx.AsyncClient
    # Maps each enabled payment method to its provider instance. Disabled
    # methods are simply absent from the dict, so callers use `.get(...)` and
    # treat None as "method not enabled".
    providers: dict[PaymentMethod, PaymentProvider]
    # Pre-encoded HMAC secret for the `attach` field (avoids re-encoding on
    # every request).
    attach_secret: bytes
    # Cached for the hot signing path.
    epay_key: str
