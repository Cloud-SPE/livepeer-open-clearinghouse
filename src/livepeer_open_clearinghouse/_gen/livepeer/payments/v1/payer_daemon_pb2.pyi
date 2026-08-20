from livepeer.payments.v1 import types_pb2 as _types_pb2
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class CreatePaymentRequest(_message.Message):
    __slots__ = (
        "recipient",
        "ticket_params_base_url",
        "accepted_price",
        "funding",
        "mint_request_id",
    )
    RECIPIENT_FIELD_NUMBER: _ClassVar[int]
    TICKET_PARAMS_BASE_URL_FIELD_NUMBER: _ClassVar[int]
    ACCEPTED_PRICE_FIELD_NUMBER: _ClassVar[int]
    FUNDING_FIELD_NUMBER: _ClassVar[int]
    MINT_REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    recipient: bytes
    ticket_params_base_url: str
    accepted_price: _types_pb2.AcceptedPrice
    funding: _types_pb2.FundingIntent
    mint_request_id: str
    def __init__(
        self,
        recipient: _Optional[bytes] = ...,
        ticket_params_base_url: _Optional[str] = ...,
        accepted_price: _Optional[_Union[_types_pb2.AcceptedPrice, _Mapping]] = ...,
        funding: _Optional[_Union[_types_pb2.FundingIntent, _Mapping]] = ...,
        mint_request_id: _Optional[str] = ...,
    ) -> None: ...

class CreatePaymentResponse(_message.Message):
    __slots__ = (
        "payment_bytes",
        "tickets_created",
        "expected_value",
        "funded_value_wei",
        "accepted_quote_ref",
        "work_id",
    )
    PAYMENT_BYTES_FIELD_NUMBER: _ClassVar[int]
    TICKETS_CREATED_FIELD_NUMBER: _ClassVar[int]
    EXPECTED_VALUE_FIELD_NUMBER: _ClassVar[int]
    FUNDED_VALUE_WEI_FIELD_NUMBER: _ClassVar[int]
    ACCEPTED_QUOTE_REF_FIELD_NUMBER: _ClassVar[int]
    WORK_ID_FIELD_NUMBER: _ClassVar[int]
    payment_bytes: bytes
    tickets_created: int
    expected_value: _types_pb2.BigUInt
    funded_value_wei: _types_pb2.BigUInt
    accepted_quote_ref: _types_pb2.QuoteRef
    work_id: str
    def __init__(
        self,
        payment_bytes: _Optional[bytes] = ...,
        tickets_created: _Optional[int] = ...,
        expected_value: _Optional[_Union[_types_pb2.BigUInt, _Mapping]] = ...,
        funded_value_wei: _Optional[_Union[_types_pb2.BigUInt, _Mapping]] = ...,
        accepted_quote_ref: _Optional[_Union[_types_pb2.QuoteRef, _Mapping]] = ...,
        work_id: _Optional[str] = ...,
    ) -> None: ...

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
    def __init__(
        self,
        work_id: _Optional[str] = ...,
        capability: _Optional[str] = ...,
        offering: _Optional[str] = ...,
        rejection_reason: _Optional[_Union[_types_pb2.PaymentRejectionReason, str]] = ...,
    ) -> None: ...

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
    def __init__(
        self,
        total_work_units: _Optional[int] = ...,
        debit_count: _Optional[int] = ...,
        closed: bool = ...,
    ) -> None: ...

class GetDepositInfoRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class GetDepositInfoResponse(_message.Message):
    __slots__ = ("deposit", "reserve", "withdraw_round")
    DEPOSIT_FIELD_NUMBER: _ClassVar[int]
    RESERVE_FIELD_NUMBER: _ClassVar[int]
    WITHDRAW_ROUND_FIELD_NUMBER: _ClassVar[int]
    deposit: bytes
    reserve: bytes
    withdraw_round: int
    def __init__(
        self,
        deposit: _Optional[bytes] = ...,
        reserve: _Optional[bytes] = ...,
        withdraw_round: _Optional[int] = ...,
    ) -> None: ...
