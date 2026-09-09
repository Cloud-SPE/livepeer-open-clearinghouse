"""gRPC client for payment-daemon over a Unix domain socket.

See `docs/references/payment-daemon.md` for the API surface Livepeer Open Clearinghouse uses.

Phase 7 wires through MockPaymentDaemonClient. The real GrpcPaymentDaemonClient
lands once `make protoc` is run and the generated stubs are committed.
"""

from livepeer_open_clearinghouse.providers.payment_daemon.client import (
    AcceptedPrice,
    AccountFundingIntent,
    CreatePaymentRequest,
    CreatePaymentResponse,
    CreateSpendAuthorizationRequest,
    CreateSpendAuthorizationResponse,
    DaemonDepositInsufficient,
    DepositInfo,
    FundingIntent,
    GrpcPaymentDaemonClient,
    InvalidRecipientRand,
    MintOutcomeUnknown,
    MockPaymentDaemonClient,
    PaymentDaemonClient,
    PaymentDaemonError,
    QuoteRef,
    validate_funding_response,
)

__all__ = [
    "AcceptedPrice",
    "AccountFundingIntent",
    "CreatePaymentRequest",
    "CreatePaymentResponse",
    "CreateSpendAuthorizationRequest",
    "CreateSpendAuthorizationResponse",
    "DaemonDepositInsufficient",
    "DepositInfo",
    "FundingIntent",
    "GrpcPaymentDaemonClient",
    "InvalidRecipientRand",
    "MintOutcomeUnknown",
    "MockPaymentDaemonClient",
    "PaymentDaemonClient",
    "PaymentDaemonError",
    "QuoteRef",
    "validate_funding_response",
]
