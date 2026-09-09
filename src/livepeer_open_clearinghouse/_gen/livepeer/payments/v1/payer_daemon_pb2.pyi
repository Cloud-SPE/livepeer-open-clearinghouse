from livepeer.payments.v1 import types_pb2 as _types_pb2
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class CreatePaymentRequest(_message.Message):
    __slots__ = ("recipient", "ticket_params_base_url", "accepted_price", "funding", "mint_request_id", "account_funding")
    RECIPIENT_FIELD_NUMBER: _ClassVar[int]
    TICKET_PARAMS_BASE_URL_FIELD_NUMBER: _ClassVar[int]
    ACCEPTED_PRICE_FIELD_NUMBER: _ClassVar[int]
    FUNDING_FIELD_NUMBER: _ClassVar[int]
    MINT_REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    ACCOUNT_FUNDING_FIELD_NUMBER: _ClassVar[int]
    recipient: bytes
    ticket_params_base_url: str
    accepted_price: _types_pb2.AcceptedPrice
    funding: _types_pb2.FundingIntent
    mint_request_id: str
    account_funding: _types_pb2.AccountFundingIntent
    def __init__(self, recipient: _Optional[bytes] = ..., ticket_params_base_url: _Optional[str] = ..., accepted_price: _Optional[_Union[_types_pb2.AcceptedPrice, _Mapping]] = ..., funding: _Optional[_Union[_types_pb2.FundingIntent, _Mapping]] = ..., mint_request_id: _Optional[str] = ..., account_funding: _Optional[_Union[_types_pb2.AccountFundingIntent, _Mapping]] = ...) -> None: ...

class CreatePaymentResponse(_message.Message):
    __slots__ = ("payment_bytes", "tickets_created", "expected_value", "funded_value_wei", "accepted_quote_ref", "work_id", "predecessor_work_id", "creation_round", "expires_after_round", "ticket_validity_period", "ticket_validity_period_observed_at", "account_shortfall_wei")
    PAYMENT_BYTES_FIELD_NUMBER: _ClassVar[int]
    TICKETS_CREATED_FIELD_NUMBER: _ClassVar[int]
    EXPECTED_VALUE_FIELD_NUMBER: _ClassVar[int]
    FUNDED_VALUE_WEI_FIELD_NUMBER: _ClassVar[int]
    ACCEPTED_QUOTE_REF_FIELD_NUMBER: _ClassVar[int]
    WORK_ID_FIELD_NUMBER: _ClassVar[int]
    PREDECESSOR_WORK_ID_FIELD_NUMBER: _ClassVar[int]
    CREATION_ROUND_FIELD_NUMBER: _ClassVar[int]
    EXPIRES_AFTER_ROUND_FIELD_NUMBER: _ClassVar[int]
    TICKET_VALIDITY_PERIOD_FIELD_NUMBER: _ClassVar[int]
    TICKET_VALIDITY_PERIOD_OBSERVED_AT_FIELD_NUMBER: _ClassVar[int]
    ACCOUNT_SHORTFALL_WEI_FIELD_NUMBER: _ClassVar[int]
    payment_bytes: bytes
    tickets_created: int
    expected_value: _types_pb2.BigUInt
    funded_value_wei: _types_pb2.BigUInt
    accepted_quote_ref: _types_pb2.QuoteRef
    work_id: str
    predecessor_work_id: str
    creation_round: int
    expires_after_round: int
    ticket_validity_period: int
    ticket_validity_period_observed_at: str
    account_shortfall_wei: _types_pb2.BigUInt
    def __init__(self, payment_bytes: _Optional[bytes] = ..., tickets_created: _Optional[int] = ..., expected_value: _Optional[_Union[_types_pb2.BigUInt, _Mapping]] = ..., funded_value_wei: _Optional[_Union[_types_pb2.BigUInt, _Mapping]] = ..., accepted_quote_ref: _Optional[_Union[_types_pb2.QuoteRef, _Mapping]] = ..., work_id: _Optional[str] = ..., predecessor_work_id: _Optional[str] = ..., creation_round: _Optional[int] = ..., expires_after_round: _Optional[int] = ..., ticket_validity_period: _Optional[int] = ..., ticket_validity_period_observed_at: _Optional[str] = ..., account_shortfall_wei: _Optional[_Union[_types_pb2.BigUInt, _Mapping]] = ...) -> None: ...

class CreateSpendAuthorizationRequest(_message.Message):
    __slots__ = ("payee", "authorization_id", "request_id", "session_id", "protocol", "accepted_price", "max_debit_wei", "max_total_units", "not_before", "expires_at", "request_digest", "caller_public_key", "revision", "predecessor_authorization_id", "broker_uri", "chain_id", "denomination")
    PAYEE_FIELD_NUMBER: _ClassVar[int]
    AUTHORIZATION_ID_FIELD_NUMBER: _ClassVar[int]
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    PROTOCOL_FIELD_NUMBER: _ClassVar[int]
    ACCEPTED_PRICE_FIELD_NUMBER: _ClassVar[int]
    MAX_DEBIT_WEI_FIELD_NUMBER: _ClassVar[int]
    MAX_TOTAL_UNITS_FIELD_NUMBER: _ClassVar[int]
    NOT_BEFORE_FIELD_NUMBER: _ClassVar[int]
    EXPIRES_AT_FIELD_NUMBER: _ClassVar[int]
    REQUEST_DIGEST_FIELD_NUMBER: _ClassVar[int]
    CALLER_PUBLIC_KEY_FIELD_NUMBER: _ClassVar[int]
    REVISION_FIELD_NUMBER: _ClassVar[int]
    PREDECESSOR_AUTHORIZATION_ID_FIELD_NUMBER: _ClassVar[int]
    BROKER_URI_FIELD_NUMBER: _ClassVar[int]
    CHAIN_ID_FIELD_NUMBER: _ClassVar[int]
    DENOMINATION_FIELD_NUMBER: _ClassVar[int]
    payee: bytes
    authorization_id: str
    request_id: str
    session_id: str
    protocol: str
    accepted_price: _types_pb2.AcceptedPrice
    max_debit_wei: _types_pb2.BigUInt
    max_total_units: int
    not_before: str
    expires_at: str
    request_digest: bytes
    caller_public_key: bytes
    revision: int
    predecessor_authorization_id: str
    broker_uri: str
    chain_id: int
    denomination: str
    def __init__(self, payee: _Optional[bytes] = ..., authorization_id: _Optional[str] = ..., request_id: _Optional[str] = ..., session_id: _Optional[str] = ..., protocol: _Optional[str] = ..., accepted_price: _Optional[_Union[_types_pb2.AcceptedPrice, _Mapping]] = ..., max_debit_wei: _Optional[_Union[_types_pb2.BigUInt, _Mapping]] = ..., max_total_units: _Optional[int] = ..., not_before: _Optional[str] = ..., expires_at: _Optional[str] = ..., request_digest: _Optional[bytes] = ..., caller_public_key: _Optional[bytes] = ..., revision: _Optional[int] = ..., predecessor_authorization_id: _Optional[str] = ..., broker_uri: _Optional[str] = ..., chain_id: _Optional[int] = ..., denomination: _Optional[str] = ...) -> None: ...

class CreateSpendAuthorizationResponse(_message.Message):
    __slots__ = ("authorization_bytes", "authorization_id", "payer")
    AUTHORIZATION_BYTES_FIELD_NUMBER: _ClassVar[int]
    AUTHORIZATION_ID_FIELD_NUMBER: _ClassVar[int]
    PAYER_FIELD_NUMBER: _ClassVar[int]
    authorization_bytes: bytes
    authorization_id: str
    payer: bytes
    def __init__(self, authorization_bytes: _Optional[bytes] = ..., authorization_id: _Optional[str] = ..., payer: _Optional[bytes] = ...) -> None: ...

class ReportPaymentResultRequest(_message.Message):
    __slots__ = ("work_id", "capability", "offering", "rejection_reason")
    WORK_ID_FIELD_NUMBER: _ClassVar[int]
    CAPABILITY_FIELD_NUMBER: _ClassVar[int]
    OFFERING_FIELD_NUMBER: _ClassVar[int]
    REJECTION_REASON_FIELD_NUMBER: _ClassVar[int]
    work_id: str
    capability: str
    offering: str
    rejection_reason: _types_pb2.PaymentRejectionReason
    def __init__(self, work_id: _Optional[str] = ..., capability: _Optional[str] = ..., offering: _Optional[str] = ..., rejection_reason: _Optional[_Union[_types_pb2.PaymentRejectionReason, str]] = ...) -> None: ...

class ReportPaymentResultResponse(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class GetSessionDebitsRequest(_message.Message):
    __slots__ = ("sender", "work_id")
    SENDER_FIELD_NUMBER: _ClassVar[int]
    WORK_ID_FIELD_NUMBER: _ClassVar[int]
    sender: bytes
    work_id: str
    def __init__(self, sender: _Optional[bytes] = ..., work_id: _Optional[str] = ...) -> None: ...

class GetSessionDebitsResponse(_message.Message):
    __slots__ = ("total_work_units", "debit_count", "closed")
    TOTAL_WORK_UNITS_FIELD_NUMBER: _ClassVar[int]
    DEBIT_COUNT_FIELD_NUMBER: _ClassVar[int]
    CLOSED_FIELD_NUMBER: _ClassVar[int]
    total_work_units: int
    debit_count: int
    closed: bool
    def __init__(self, total_work_units: _Optional[int] = ..., debit_count: _Optional[int] = ..., closed: bool = ...) -> None: ...

class GetDepositInfoRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class GetDepositInfoResponse(_message.Message):
    __slots__ = ("deposit", "reserve", "withdraw_round", "current_round", "ticket_validity_period", "ticket_validity_period_observed_at")
    DEPOSIT_FIELD_NUMBER: _ClassVar[int]
    RESERVE_FIELD_NUMBER: _ClassVar[int]
    WITHDRAW_ROUND_FIELD_NUMBER: _ClassVar[int]
    CURRENT_ROUND_FIELD_NUMBER: _ClassVar[int]
    TICKET_VALIDITY_PERIOD_FIELD_NUMBER: _ClassVar[int]
    TICKET_VALIDITY_PERIOD_OBSERVED_AT_FIELD_NUMBER: _ClassVar[int]
    deposit: bytes
    reserve: bytes
    withdraw_round: int
    current_round: int
    ticket_validity_period: int
    ticket_validity_period_observed_at: str
    def __init__(self, deposit: _Optional[bytes] = ..., reserve: _Optional[bytes] = ..., withdraw_round: _Optional[int] = ..., current_round: _Optional[int] = ..., ticket_validity_period: _Optional[int] = ..., ticket_validity_period_observed_at: _Optional[str] = ...) -> None: ...
