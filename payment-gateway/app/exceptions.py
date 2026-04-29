"""Domain exceptions.

All gateway-level errors inherit from `GatewayError`, which carries a
`public_message` separate from the (potentially sensitive) internal
exception args. The middleware-level handler returns the public message
to clients, while the full detail goes to the log.
"""

from __future__ import annotations


class GatewayError(Exception):
    """Base for all gateway-internal errors. Never expose `args[0]` to clients."""

    public_message: str = "internal error"

    def __init__(self, internal: str = "", public: str | None = None) -> None:
        super().__init__(internal or self.__class__.__name__)
        if public:
            self.public_message = public


class InvalidEpaySignature(GatewayError):
    public_message = "invalid signature"


class InvalidAttach(GatewayError):
    public_message = "invalid attach"


class ProviderError(GatewayError):
    public_message = "provider error"


class ProviderDisabled(GatewayError):
    public_message = "payment method not enabled"


class UpstreamNotifyFailed(GatewayError):
    """new-api refused / didn't ack the notify; signal the payment platform to retry."""

    public_message = "upstream not acknowledged"
