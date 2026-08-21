"""Verification of broker-signed Modules v2 settlement envelopes."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import rfc8785
from eth_hash.auto import keccak
from eth_keys.datatypes import Signature
from eth_keys.exceptions import BadSignature
from google.protobuf import json_format
from google.protobuf.message import DecodeError

from livepeer_open_clearinghouse import _gen  # noqa: F401

_SIGNATURE_BYTES = 65


class SettlementVerificationError(ValueError):
    """The settlement cannot authorize a financial state change."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class JobSettlementExpectation:
    request_id: str
    job_id: str
    work_id: str
    work_unit: str
    actual_units: int
    amount_wei: int
    per_units: int
    quote_id: str
    quote_version: int
    constraint_fingerprint: bytes
    route_fingerprint: bytes


@dataclass(frozen=True, slots=True)
class VerifiedJobSettlement:
    actual_units: int
    billed_value_wei: int
    outcome: str
    issued_at: datetime
    signing_public_key: str


@dataclass(frozen=True, slots=True)
class SessionSettlementExpectation:
    gateway_session_id: str
    broker_session_id: str | None
    work_id: str
    predecessor_work_id: str
    rotation_generation: int
    work_unit: str
    amount_wei: int
    per_units: int
    funded_value_wei: int
    last_settlement_seq: int
    require_terminal: bool = True


@dataclass(frozen=True, slots=True)
class VerifiedSessionSettlement:
    broker_session_id: str
    work_id: str
    predecessor_work_id: str
    rotation_generation: int
    settlement_seq: int
    state: str
    claimed_units: int
    debited_units: int
    billed_value_wei: int
    outcome: str
    issued_at: datetime
    signing_public_key: str


def verify_job_settlement(
    envelope: Mapping[str, Any],
    *,
    settlement_keys: Sequence[Mapping[str, Any]],
    expected: JobSettlementExpectation,
) -> VerifiedJobSettlement:
    """Verify signature, delegation, identity, quote, units, and arithmetic."""

    record, issued_at, public_key = _verify_envelope(envelope, settlement_keys)
    _reject_failed_debit(record)
    if record.request_id != expected.request_id:
        raise SettlementVerificationError(
            "request_id_mismatch", "signed gateway request id does not match"
        )
    if record.job_id != expected.job_id:
        raise SettlementVerificationError("job_id_mismatch", "signed job_id does not match")
    if record.work_id != expected.work_id:
        raise SettlementVerificationError("work_id_mismatch", "signed work_id does not match")
    if record.work_unit_name != expected.work_unit:
        raise SettlementVerificationError("work_unit_mismatch", "signed work unit does not match")
    if (
        record.actual_units != expected.actual_units
        or record.billed_units != expected.actual_units
        or record.debited_units != expected.actual_units
    ):
        raise SettlementVerificationError("work_units_mismatch", "signed work units do not match")

    quote = record.accepted_quote_ref
    if (
        quote.quote_id != expected.quote_id
        or quote.quote_version != expected.quote_version
        or bytes(quote.constraint_fingerprint) != expected.constraint_fingerprint
        or bytes(quote.route_fingerprint) != expected.route_fingerprint
    ):
        raise SettlementVerificationError("quote_mismatch", "signed quote reference does not match")

    billed_value = int.from_bytes(record.billed_value_wei.value, "big")
    cumulative_units = record.payment_cumulative_units
    if cumulative_units < record.debited_units:
        raise SettlementVerificationError(
            "payment_curve_invalid", "payment cumulative units precede this job's debit"
        )
    normative_bill = _bill(cumulative_units, expected.amount_wei, expected.per_units) - _bill(
        cumulative_units - record.debited_units,
        expected.amount_wei,
        expected.per_units,
    )
    if billed_value != normative_bill:
        raise SettlementVerificationError(
            "billed_value_mismatch", "signed billed value does not match normative bill(U)"
        )

    return VerifiedJobSettlement(
        actual_units=record.actual_units,
        billed_value_wei=billed_value,
        outcome=record.SettlementOutcome.Name(record.outcome),
        issued_at=issued_at,
        signing_public_key=public_key,
    )


def verify_session_settlement(
    envelope: Mapping[str, Any],
    *,
    settlement_keys: Sequence[Mapping[str, Any]],
    expected: SessionSettlementExpectation,
) -> VerifiedSessionSettlement:
    """Verify a paid-session settlement before it changes LOC accounting."""

    record, issued_at, public_key = _verify_envelope(envelope, settlement_keys)
    _reject_failed_debit(record)
    _verify_session_identity(record, expected)
    billed_value = _verify_session_accounting(record, expected)

    return VerifiedSessionSettlement(
        broker_session_id=record.session_id,
        work_id=record.work_id,
        predecessor_work_id=record.predecessor_work_id,
        rotation_generation=record.rotation_generation,
        settlement_seq=record.settlement_seq,
        state=record.state,
        claimed_units=record.claimed_units,
        debited_units=record.debited_units,
        billed_value_wei=billed_value,
        outcome=record.SettlementOutcome.Name(record.outcome),
        issued_at=issued_at,
        signing_public_key=public_key,
    )


def _reject_failed_debit(record: Any) -> None:
    """Refuse evidence that explicitly says the ledger did not settle."""

    if record.outcome == record.DEBIT_FAILED:
        raise SettlementVerificationError(
            "debit_failed", "broker settlement reports that the ledger debit failed"
        )


def _verify_session_identity(record: Any, expected: SessionSettlementExpectation) -> None:
    if record.gateway_session_id != expected.gateway_session_id:
        raise SettlementVerificationError(
            "gateway_session_id_mismatch", "signed gateway session id does not match"
        )
    if not record.session_id:
        raise SettlementVerificationError("missing_session_id", "signed session id is required")
    if expected.broker_session_id is not None and record.session_id != expected.broker_session_id:
        raise SettlementVerificationError("session_id_mismatch", "signed session id forked")
    if record.job_id:
        raise SettlementVerificationError("job_id_present", "session settlement contains job_id")
    if record.work_id != expected.work_id:
        raise SettlementVerificationError("work_id_mismatch", "signed work id does not match")
    if record.predecessor_work_id != expected.predecessor_work_id:
        raise SettlementVerificationError(
            "predecessor_mismatch", "signed predecessor work id does not match"
        )
    if record.rotation_generation != expected.rotation_generation:
        raise SettlementVerificationError(
            "rotation_generation_mismatch", "signed rotation generation does not match"
        )
    if record.work_unit_name != expected.work_unit:
        raise SettlementVerificationError("work_unit_mismatch", "signed work unit does not match")


def _verify_session_accounting(record: Any, expected: SessionSettlementExpectation) -> int:

    amount_wei = int.from_bytes(record.amount_wei.value, "big")
    if amount_wei != expected.amount_wei or record.per_units != expected.per_units:
        raise SettlementVerificationError("pricing_mismatch", "signed session price does not match")
    if record.settlement_seq <= expected.last_settlement_seq:
        raise SettlementVerificationError(
            "settlement_replay", "signed settlement sequence is not newer"
        )
    if expected.require_terminal and record.state != "closed":
        raise SettlementVerificationError(
            "session_not_terminal", "signed settlement is not terminal"
        )
    if record.claimed_units != record.debited_units:
        raise SettlementVerificationError(
            "claim_debit_gap", "signed claimed and debited units diverge"
        )
    if record.actual_units != record.debited_units or record.billed_units != record.debited_units:
        raise SettlementVerificationError(
            "work_units_mismatch", "signed session unit totals diverge"
        )
    if record.generation_debited_units > record.debited_units:
        raise SettlementVerificationError(
            "generation_units_invalid", "generation units exceed session units"
        )

    billed_value = int.from_bytes(record.billed_value_wei.value, "big")
    signed_funded = int.from_bytes(record.funded_value_wei.value, "big")
    generation_billed = int.from_bytes(record.generation_billed_value_wei.value, "big")
    if billed_value > expected.funded_value_wei or signed_funded > expected.funded_value_wei:
        raise SettlementVerificationError(
            "session_cap_exceeded", "signed settlement exceeds LOC session funding"
        )
    if generation_billed > billed_value:
        raise SettlementVerificationError(
            "generation_value_invalid", "generation billed value exceeds session billed value"
        )
    return billed_value


def _verify_envelope(
    envelope: Mapping[str, Any], settlement_keys: Sequence[Mapping[str, Any]]
) -> tuple[Any, datetime, str]:
    payload = envelope.get("payload")
    signature = envelope.get("signature")
    if not isinstance(payload, dict) or not isinstance(signature, dict):
        raise SettlementVerificationError(
            "malformed_envelope", "payload and signature are required"
        )
    if signature.get("algorithm") != "secp256k1" or signature.get("canonicalization") != "jcs":
        raise SettlementVerificationError(
            "unsupported_signature", "unsupported settlement signature scheme"
        )
    canonical = _canonicalize(payload)
    public_key = _recover_public_key(canonical, signature.get("value"))
    issued_at = _parse_issued_at(payload.get("issued_at"))
    _authorize_key(public_key, issued_at, settlement_keys)
    return _parse_record(payload), issued_at, public_key


def _canonicalize(payload: dict[str, Any]) -> bytes:
    try:
        return rfc8785.dumps(payload)
    except (TypeError, ValueError, rfc8785.CanonicalizationError) as exc:
        raise SettlementVerificationError("invalid_canonical_payload", str(exc)) from exc


def _recover_public_key(canonical: bytes, value: object) -> str:
    if not isinstance(value, str) or not value.startswith("0x"):
        raise SettlementVerificationError("malformed_signature", "signature must be 0x-prefixed")
    try:
        raw = bytearray.fromhex(value[2:])
    except ValueError as exc:
        raise SettlementVerificationError("malformed_signature", "signature is not hex") from exc
    if len(raw) != _SIGNATURE_BYTES or raw[64] not in (27, 28):
        raise SettlementVerificationError(
            "malformed_signature", "signature must be 65 bytes with v 27/28"
        )
    raw[64] -= 27
    prefix = f"\x19Ethereum Signed Message:\n{len(canonical)}".encode()
    try:
        recovered = Signature(bytes(raw)).recover_public_key_from_msg_hash(
            keccak(prefix + canonical)
        )
    except BadSignature as exc:
        raise SettlementVerificationError("invalid_signature", "signature recovery failed") from exc
    return "0x04" + recovered.to_bytes().hex()


def _parse_issued_at(value: object) -> datetime:
    if not isinstance(value, str):
        raise SettlementVerificationError("missing_issued_at", "signed issued_at is required")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SettlementVerificationError(
            "invalid_issued_at", "signed issued_at is invalid"
        ) from exc
    if parsed.tzinfo is None:
        raise SettlementVerificationError(
            "invalid_issued_at", "signed issued_at must be timezone-aware"
        )
    return parsed


def _authorize_key(
    public_key: str, issued_at: datetime, settlement_keys: Sequence[Mapping[str, Any]]
) -> None:
    for delegation in settlement_keys:
        if delegation.get("public_key") != public_key:
            continue
        try:
            not_before = datetime.fromisoformat(
                str(delegation["not_before"]).replace("Z", "+00:00")
            )
            expires_at = datetime.fromisoformat(
                str(delegation["expires_at"]).replace("Z", "+00:00")
            )
        except (KeyError, ValueError) as exc:
            raise SettlementVerificationError(
                "invalid_delegation", "pinned delegation is invalid"
            ) from exc
        if not_before <= issued_at <= expires_at:
            return
    raise SettlementVerificationError(
        "unauthorized_signing_key", "signing key was not delegated at issued_at"
    )


def _parse_record(payload: dict[str, Any]):  # type: ignore[no-untyped-def]
    from livepeer.payments.v1 import types_pb2  # noqa: PLC0415

    try:
        return json_format.ParseDict(payload, types_pb2.SettlementRecord())
    except (json_format.ParseError, DecodeError, ValueError) as exc:
        raise SettlementVerificationError("malformed_payload", "invalid settlement record") from exc


def _bill(units: int, amount_wei: int, per_units: int) -> int:
    if units < 0 or amount_wei < 0 or per_units <= 0:
        raise SettlementVerificationError("invalid_pricing", "invalid pinned pricing")
    return (units * amount_wei + per_units - 1) // per_units
