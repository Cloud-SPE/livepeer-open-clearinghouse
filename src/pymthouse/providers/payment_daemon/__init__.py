"""gRPC client for payment-daemon over a Unix domain socket.

See `docs/references/payment-daemon.md` for the API surface PymtHouse uses.

Phase 7 wires through MockPaymentDaemonClient. The real GrpcPaymentDaemonClient
lands once `make protoc` is run and the generated stubs are committed.
"""

from pymthouse.providers.payment_daemon.client import (
    AcceptedPrice,
    CreatePaymentRequest,
    CreatePaymentResponse,
    DaemonDepositInsufficient,
    FundingIntent,
    GrpcPaymentDaemonClient,
    InvalidRecipientRand,
    MockPaymentDaemonClient,
    PaymentDaemonClient,
    PaymentDaemonError,
    QuoteRef,
)

__all__ = [
    "AcceptedPrice",
    "CreatePaymentRequest",
    "CreatePaymentResponse",
    "DaemonDepositInsufficient",
    "FundingIntent",
    "GrpcPaymentDaemonClient",
    "InvalidRecipientRand",
    "MockPaymentDaemonClient",
    "PaymentDaemonClient",
    "PaymentDaemonError",
    "QuoteRef",
]
