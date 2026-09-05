from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class PaymentRejectionReason(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    PAYMENT_REJECTION_REASON_UNSPECIFIED: _ClassVar[PaymentRejectionReason]
    PAYMENT_REJECTION_REASON_INVALID_RECIPIENT_RAND: _ClassVar[PaymentRejectionReason]
    PAYMENT_REJECTION_REASON_NONCE_REPLAY: _ClassVar[PaymentRejectionReason]
    PAYMENT_REJECTION_REASON_NONCE_CAP_REACHED: _ClassVar[PaymentRejectionReason]
    PAYMENT_REJECTION_REASON_INVALID_SIGNATURE: _ClassVar[PaymentRejectionReason]
    PAYMENT_REJECTION_REASON_OTHER: _ClassVar[PaymentRejectionReason]
PAYMENT_REJECTION_REASON_UNSPECIFIED: PaymentRejectionReason
PAYMENT_REJECTION_REASON_INVALID_RECIPIENT_RAND: PaymentRejectionReason
PAYMENT_REJECTION_REASON_NONCE_REPLAY: PaymentRejectionReason
PAYMENT_REJECTION_REASON_NONCE_CAP_REACHED: PaymentRejectionReason
PAYMENT_REJECTION_REASON_INVALID_SIGNATURE: PaymentRejectionReason
PAYMENT_REJECTION_REASON_OTHER: PaymentRejectionReason

class PriceInfo(_message.Message):
    __slots__ = ("price_per_unit", "pixels_per_unit", "capability", "constraint")
    PRICE_PER_UNIT_FIELD_NUMBER: _ClassVar[int]
    PIXELS_PER_UNIT_FIELD_NUMBER: _ClassVar[int]
    CAPABILITY_FIELD_NUMBER: _ClassVar[int]
    CONSTRAINT_FIELD_NUMBER: _ClassVar[int]
    price_per_unit: int
    pixels_per_unit: int
    capability: int
    constraint: str
    def __init__(self, price_per_unit: _Optional[int] = ..., pixels_per_unit: _Optional[int] = ..., capability: _Optional[int] = ..., constraint: _Optional[str] = ...) -> None: ...

class TicketParams(_message.Message):
    __slots__ = ("recipient", "face_value", "win_prob", "recipient_rand_hash", "seed", "expiration_block", "expiration_params")
    RECIPIENT_FIELD_NUMBER: _ClassVar[int]
    FACE_VALUE_FIELD_NUMBER: _ClassVar[int]
    WIN_PROB_FIELD_NUMBER: _ClassVar[int]
    RECIPIENT_RAND_HASH_FIELD_NUMBER: _ClassVar[int]
    SEED_FIELD_NUMBER: _ClassVar[int]
    EXPIRATION_BLOCK_FIELD_NUMBER: _ClassVar[int]
    EXPIRATION_PARAMS_FIELD_NUMBER: _ClassVar[int]
    recipient: bytes
    face_value: bytes
    win_prob: bytes
    recipient_rand_hash: bytes
    seed: bytes
    expiration_block: bytes
    expiration_params: TicketExpirationParams
    def __init__(self, recipient: _Optional[bytes] = ..., face_value: _Optional[bytes] = ..., win_prob: _Optional[bytes] = ..., recipient_rand_hash: _Optional[bytes] = ..., seed: _Optional[bytes] = ..., expiration_block: _Optional[bytes] = ..., expiration_params: _Optional[_Union[TicketExpirationParams, _Mapping]] = ...) -> None: ...

class TicketSenderParams(_message.Message):
    __slots__ = ("sender_nonce", "sig")
    SENDER_NONCE_FIELD_NUMBER: _ClassVar[int]
    SIG_FIELD_NUMBER: _ClassVar[int]
    sender_nonce: int
    sig: bytes
    def __init__(self, sender_nonce: _Optional[int] = ..., sig: _Optional[bytes] = ...) -> None: ...

class TicketExpirationParams(_message.Message):
    __slots__ = ("creation_round", "creation_round_block_hash")
    CREATION_ROUND_FIELD_NUMBER: _ClassVar[int]
    CREATION_ROUND_BLOCK_HASH_FIELD_NUMBER: _ClassVar[int]
    creation_round: int
    creation_round_block_hash: bytes
    def __init__(self, creation_round: _Optional[int] = ..., creation_round_block_hash: _Optional[bytes] = ...) -> None: ...

class Payment(_message.Message):
    __slots__ = ("ticket_params", "sender", "expiration_params", "ticket_sender_params", "expected_price")
    TICKET_PARAMS_FIELD_NUMBER: _ClassVar[int]
    SENDER_FIELD_NUMBER: _ClassVar[int]
    EXPIRATION_PARAMS_FIELD_NUMBER: _ClassVar[int]
    TICKET_SENDER_PARAMS_FIELD_NUMBER: _ClassVar[int]
    EXPECTED_PRICE_FIELD_NUMBER: _ClassVar[int]
    ticket_params: TicketParams
    sender: bytes
    expiration_params: TicketExpirationParams
    ticket_sender_params: _containers.RepeatedCompositeFieldContainer[TicketSenderParams]
    expected_price: PriceInfo
    def __init__(self, ticket_params: _Optional[_Union[TicketParams, _Mapping]] = ..., sender: _Optional[bytes] = ..., expiration_params: _Optional[_Union[TicketExpirationParams, _Mapping]] = ..., ticket_sender_params: _Optional[_Iterable[_Union[TicketSenderParams, _Mapping]]] = ..., expected_price: _Optional[_Union[PriceInfo, _Mapping]] = ...) -> None: ...

class OfferingPrice(_message.Message):
    __slots__ = ("id", "price_info")
    ID_FIELD_NUMBER: _ClassVar[int]
    PRICE_INFO_FIELD_NUMBER: _ClassVar[int]
    id: str
    price_info: PriceInfo
    def __init__(self, id: _Optional[str] = ..., price_info: _Optional[_Union[PriceInfo, _Mapping]] = ...) -> None: ...

class CapabilityEntry(_message.Message):
    __slots__ = ("capability", "work_unit", "offerings")
    CAPABILITY_FIELD_NUMBER: _ClassVar[int]
    WORK_UNIT_FIELD_NUMBER: _ClassVar[int]
    OFFERINGS_FIELD_NUMBER: _ClassVar[int]
    capability: str
    work_unit: str
    offerings: _containers.RepeatedCompositeFieldContainer[OfferingPrice]
    def __init__(self, capability: _Optional[str] = ..., work_unit: _Optional[str] = ..., offerings: _Optional[_Iterable[_Union[OfferingPrice, _Mapping]]] = ...) -> None: ...

class HealthRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class HealthResponse(_message.Message):
    __slots__ = ("status",)
    STATUS_FIELD_NUMBER: _ClassVar[int]
    status: str
    def __init__(self, status: _Optional[str] = ...) -> None: ...

class BigUInt(_message.Message):
    __slots__ = ("value",)
    VALUE_FIELD_NUMBER: _ClassVar[int]
    value: bytes
    def __init__(self, value: _Optional[bytes] = ...) -> None: ...

class QuoteRef(_message.Message):
    __slots__ = ("quote_id", "quote_version", "constraint_fingerprint", "route_fingerprint")
    QUOTE_ID_FIELD_NUMBER: _ClassVar[int]
    QUOTE_VERSION_FIELD_NUMBER: _ClassVar[int]
    CONSTRAINT_FINGERPRINT_FIELD_NUMBER: _ClassVar[int]
    ROUTE_FINGERPRINT_FIELD_NUMBER: _ClassVar[int]
    quote_id: str
    quote_version: int
    constraint_fingerprint: bytes
    route_fingerprint: bytes
    def __init__(self, quote_id: _Optional[str] = ..., quote_version: _Optional[int] = ..., constraint_fingerprint: _Optional[bytes] = ..., route_fingerprint: _Optional[bytes] = ...) -> None: ...

class AcceptedPrice(_message.Message):
    __slots__ = ("price_per_unit_wei", "units_per_price", "work_unit_name", "capability", "offering", "quote_ref")
    PRICE_PER_UNIT_WEI_FIELD_NUMBER: _ClassVar[int]
    UNITS_PER_PRICE_FIELD_NUMBER: _ClassVar[int]
    WORK_UNIT_NAME_FIELD_NUMBER: _ClassVar[int]
    CAPABILITY_FIELD_NUMBER: _ClassVar[int]
    OFFERING_FIELD_NUMBER: _ClassVar[int]
    QUOTE_REF_FIELD_NUMBER: _ClassVar[int]
    price_per_unit_wei: BigUInt
    units_per_price: int
    work_unit_name: str
    capability: str
    offering: str
    quote_ref: QuoteRef
    def __init__(self, price_per_unit_wei: _Optional[_Union[BigUInt, _Mapping]] = ..., units_per_price: _Optional[int] = ..., work_unit_name: _Optional[str] = ..., capability: _Optional[str] = ..., offering: _Optional[str] = ..., quote_ref: _Optional[_Union[QuoteRef, _Mapping]] = ...) -> None: ...

class FundingIntent(_message.Message):
    __slots__ = ("estimated_units", "funded_value_wei", "max_total_units", "top_up_allowed")
    ESTIMATED_UNITS_FIELD_NUMBER: _ClassVar[int]
    FUNDED_VALUE_WEI_FIELD_NUMBER: _ClassVar[int]
    MAX_TOTAL_UNITS_FIELD_NUMBER: _ClassVar[int]
    TOP_UP_ALLOWED_FIELD_NUMBER: _ClassVar[int]
    estimated_units: int
    funded_value_wei: BigUInt
    max_total_units: int
    top_up_allowed: bool
    def __init__(self, estimated_units: _Optional[int] = ..., funded_value_wei: _Optional[_Union[BigUInt, _Mapping]] = ..., max_total_units: _Optional[int] = ..., top_up_allowed: bool = ...) -> None: ...

class SettlementRecord(_message.Message):
    __slots__ = ("accepted_quote_ref", "work_unit_name", "estimated_units", "actual_units", "billed_units", "funded_value_wei", "billed_value_wei", "outcome", "breakdown", "session_id", "work_id", "predecessor_work_id", "rotation_generation", "claimed_units", "debited_units", "generation_debited_units", "generation_billed_value_wei", "generation_funded_value_wei", "amount_wei", "per_units", "settlement_seq", "issued_at", "state", "job_id", "payment_cumulative_units", "gateway_session_id", "request_id")
    class SettlementOutcome(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
        __slots__ = ()
        SETTLEMENT_OUTCOME_UNSPECIFIED: _ClassVar[SettlementRecord.SettlementOutcome]
        EXACT: _ClassVar[SettlementRecord.SettlementOutcome]
        UNDERFUNDED: _ClassVar[SettlementRecord.SettlementOutcome]
        OVERFUNDED: _ClassVar[SettlementRecord.SettlementOutcome]
        STOPPED_AT_BUDGET: _ClassVar[SettlementRecord.SettlementOutcome]
        TOPPED_UP: _ClassVar[SettlementRecord.SettlementOutcome]
        DEBIT_FAILED: _ClassVar[SettlementRecord.SettlementOutcome]
    SETTLEMENT_OUTCOME_UNSPECIFIED: SettlementRecord.SettlementOutcome
    EXACT: SettlementRecord.SettlementOutcome
    UNDERFUNDED: SettlementRecord.SettlementOutcome
    OVERFUNDED: SettlementRecord.SettlementOutcome
    STOPPED_AT_BUDGET: SettlementRecord.SettlementOutcome
    TOPPED_UP: SettlementRecord.SettlementOutcome
    DEBIT_FAILED: SettlementRecord.SettlementOutcome
    class BreakdownEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    ACCEPTED_QUOTE_REF_FIELD_NUMBER: _ClassVar[int]
    WORK_UNIT_NAME_FIELD_NUMBER: _ClassVar[int]
    ESTIMATED_UNITS_FIELD_NUMBER: _ClassVar[int]
    ACTUAL_UNITS_FIELD_NUMBER: _ClassVar[int]
    BILLED_UNITS_FIELD_NUMBER: _ClassVar[int]
    FUNDED_VALUE_WEI_FIELD_NUMBER: _ClassVar[int]
    BILLED_VALUE_WEI_FIELD_NUMBER: _ClassVar[int]
    OUTCOME_FIELD_NUMBER: _ClassVar[int]
    BREAKDOWN_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    WORK_ID_FIELD_NUMBER: _ClassVar[int]
    PREDECESSOR_WORK_ID_FIELD_NUMBER: _ClassVar[int]
    ROTATION_GENERATION_FIELD_NUMBER: _ClassVar[int]
    CLAIMED_UNITS_FIELD_NUMBER: _ClassVar[int]
    DEBITED_UNITS_FIELD_NUMBER: _ClassVar[int]
    GENERATION_DEBITED_UNITS_FIELD_NUMBER: _ClassVar[int]
    GENERATION_BILLED_VALUE_WEI_FIELD_NUMBER: _ClassVar[int]
    GENERATION_FUNDED_VALUE_WEI_FIELD_NUMBER: _ClassVar[int]
    AMOUNT_WEI_FIELD_NUMBER: _ClassVar[int]
    PER_UNITS_FIELD_NUMBER: _ClassVar[int]
    SETTLEMENT_SEQ_FIELD_NUMBER: _ClassVar[int]
    ISSUED_AT_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    JOB_ID_FIELD_NUMBER: _ClassVar[int]
    PAYMENT_CUMULATIVE_UNITS_FIELD_NUMBER: _ClassVar[int]
    GATEWAY_SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    accepted_quote_ref: QuoteRef
    work_unit_name: str
    estimated_units: int
    actual_units: int
    billed_units: int
    funded_value_wei: BigUInt
    billed_value_wei: BigUInt
    outcome: SettlementRecord.SettlementOutcome
    breakdown: _containers.ScalarMap[str, str]
    session_id: str
    work_id: str
    predecessor_work_id: str
    rotation_generation: int
    claimed_units: int
    debited_units: int
    generation_debited_units: int
    generation_billed_value_wei: BigUInt
    generation_funded_value_wei: BigUInt
    amount_wei: BigUInt
    per_units: int
    settlement_seq: int
    issued_at: str
    state: str
    job_id: str
    payment_cumulative_units: int
    gateway_session_id: str
    request_id: str
    def __init__(self, accepted_quote_ref: _Optional[_Union[QuoteRef, _Mapping]] = ..., work_unit_name: _Optional[str] = ..., estimated_units: _Optional[int] = ..., actual_units: _Optional[int] = ..., billed_units: _Optional[int] = ..., funded_value_wei: _Optional[_Union[BigUInt, _Mapping]] = ..., billed_value_wei: _Optional[_Union[BigUInt, _Mapping]] = ..., outcome: _Optional[_Union[SettlementRecord.SettlementOutcome, str]] = ..., breakdown: _Optional[_Mapping[str, str]] = ..., session_id: _Optional[str] = ..., work_id: _Optional[str] = ..., predecessor_work_id: _Optional[str] = ..., rotation_generation: _Optional[int] = ..., claimed_units: _Optional[int] = ..., debited_units: _Optional[int] = ..., generation_debited_units: _Optional[int] = ..., generation_billed_value_wei: _Optional[_Union[BigUInt, _Mapping]] = ..., generation_funded_value_wei: _Optional[_Union[BigUInt, _Mapping]] = ..., amount_wei: _Optional[_Union[BigUInt, _Mapping]] = ..., per_units: _Optional[int] = ..., settlement_seq: _Optional[int] = ..., issued_at: _Optional[str] = ..., state: _Optional[str] = ..., job_id: _Optional[str] = ..., payment_cumulative_units: _Optional[int] = ..., gateway_session_id: _Optional[str] = ..., request_id: _Optional[str] = ...) -> None: ...

class NonAdmissionRecord(_message.Message):
    __slots__ = ("protocol", "request_id", "work_id", "sender", "recipient", "accepted_quote_ref", "broker_eth_address", "observed_at", "coverage_started_at", "outcome")
    class Outcome(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
        __slots__ = ()
        OUTCOME_UNSPECIFIED: _ClassVar[NonAdmissionRecord.Outcome]
        NOT_ADMITTED: _ClassVar[NonAdmissionRecord.Outcome]
    OUTCOME_UNSPECIFIED: NonAdmissionRecord.Outcome
    NOT_ADMITTED: NonAdmissionRecord.Outcome
    PROTOCOL_FIELD_NUMBER: _ClassVar[int]
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    WORK_ID_FIELD_NUMBER: _ClassVar[int]
    SENDER_FIELD_NUMBER: _ClassVar[int]
    RECIPIENT_FIELD_NUMBER: _ClassVar[int]
    ACCEPTED_QUOTE_REF_FIELD_NUMBER: _ClassVar[int]
    BROKER_ETH_ADDRESS_FIELD_NUMBER: _ClassVar[int]
    OBSERVED_AT_FIELD_NUMBER: _ClassVar[int]
    COVERAGE_STARTED_AT_FIELD_NUMBER: _ClassVar[int]
    OUTCOME_FIELD_NUMBER: _ClassVar[int]
    protocol: str
    request_id: str
    work_id: str
    sender: bytes
    recipient: bytes
    accepted_quote_ref: QuoteRef
    broker_eth_address: str
    observed_at: str
    coverage_started_at: str
    outcome: NonAdmissionRecord.Outcome
    def __init__(self, protocol: _Optional[str] = ..., request_id: _Optional[str] = ..., work_id: _Optional[str] = ..., sender: _Optional[bytes] = ..., recipient: _Optional[bytes] = ..., accepted_quote_ref: _Optional[_Union[QuoteRef, _Mapping]] = ..., broker_eth_address: _Optional[str] = ..., observed_at: _Optional[str] = ..., coverage_started_at: _Optional[str] = ..., outcome: _Optional[_Union[NonAdmissionRecord.Outcome, str]] = ...) -> None: ...

class TicketStatus(_message.Message):
    __slots__ = ("sender_nonce", "rejection_reason", "credited_ev", "was_winning")
    SENDER_NONCE_FIELD_NUMBER: _ClassVar[int]
    REJECTION_REASON_FIELD_NUMBER: _ClassVar[int]
    CREDITED_EV_FIELD_NUMBER: _ClassVar[int]
    WAS_WINNING_FIELD_NUMBER: _ClassVar[int]
    sender_nonce: int
    rejection_reason: PaymentRejectionReason
    credited_ev: bytes
    was_winning: bool
    def __init__(self, sender_nonce: _Optional[int] = ..., rejection_reason: _Optional[_Union[PaymentRejectionReason, str]] = ..., credited_ev: _Optional[bytes] = ..., was_winning: bool = ...) -> None: ...

class PendingRedemption(_message.Message):
    __slots__ = ("ticket_hash", "sender", "face_value", "queued_at", "attempts")
    TICKET_HASH_FIELD_NUMBER: _ClassVar[int]
    SENDER_FIELD_NUMBER: _ClassVar[int]
    FACE_VALUE_FIELD_NUMBER: _ClassVar[int]
    QUEUED_AT_FIELD_NUMBER: _ClassVar[int]
    ATTEMPTS_FIELD_NUMBER: _ClassVar[int]
    ticket_hash: bytes
    sender: bytes
    face_value: bytes
    queued_at: int
    attempts: int
    def __init__(self, ticket_hash: _Optional[bytes] = ..., sender: _Optional[bytes] = ..., face_value: _Optional[bytes] = ..., queued_at: _Optional[int] = ..., attempts: _Optional[int] = ...) -> None: ...
