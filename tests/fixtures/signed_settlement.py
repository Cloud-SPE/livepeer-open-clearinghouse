"""Builders for broker-signed paid-job/v1 settlement test records."""

from __future__ import annotations

import base64
from typing import Any

import rfc8785
from eth_hash.auto import keccak
from eth_keys.datatypes import PrivateKey

TEST_PRIVATE_KEY = PrivateKey(b"\x01" * 32)
TEST_PUBLIC_KEY = "0x04" + TEST_PRIVATE_KEY.public_key.to_bytes().hex()
TEST_ISSUED_AT = "2026-08-20T12:00:00Z"


def delegated_key(
    *,
    public_key: str = TEST_PUBLIC_KEY,
    not_before: str = "2026-08-20T00:00:00Z",
    expires_at: str = "2026-08-21T00:00:00Z",
) -> dict[str, Any]:
    return {
        "public_key": public_key,
        "not_before": not_before,
        "expires_at": expires_at,
        "introduced_in_publication_seq": 1,
    }


def signed_job_settlement(
    *,
    job_id: str,
    work_id: str,
    actual_units: int,
    debited_units: int | None = None,
    payment_cumulative_units: int | None = None,
    amount_wei: int,
    per_units: int,
    work_unit: str = "token",
    quote_id: str = "q-1",
    quote_version: int = 1,
    constraint_fingerprint: bytes = b"\x00" * 32,
    route_fingerprint: bytes = b"\x11" * 32,
    issued_at: str = TEST_ISSUED_AT,
    outcome: str = "OVERFUNDED",
    private_key: PrivateKey = TEST_PRIVATE_KEY,
) -> dict[str, Any]:
    debited = actual_units if debited_units is None else debited_units
    cumulative = actual_units if payment_cumulative_units is None else payment_cumulative_units
    prior_cumulative = max(cumulative - debited, 0)
    billed_value = (cumulative * amount_wei + per_units - 1) // per_units - (
        prior_cumulative * amount_wei + per_units - 1
    ) // per_units
    payload: dict[str, Any] = {
        "accepted_quote_ref": {
            "quote_id": quote_id,
            "quote_version": str(quote_version),
            "constraint_fingerprint": base64.b64encode(constraint_fingerprint).decode(),
            "route_fingerprint": base64.b64encode(route_fingerprint).decode(),
        },
        "work_unit_name": work_unit,
        "estimated_units": str(actual_units),
        "actual_units": str(actual_units),
        "billed_units": str(actual_units),
        "debited_units": str(debited),
        "payment_cumulative_units": str(cumulative),
        "funded_value_wei": {"value": base64.b64encode(b"\x01").decode()},
        "billed_value_wei": {"value": base64.b64encode(_unsigned_bytes(billed_value)).decode()},
        "outcome": outcome,
        "work_id": work_id,
        "issued_at": issued_at,
        "job_id": job_id,
    }
    canonical = rfc8785.dumps(payload)
    prefix = f"\x19Ethereum Signed Message:\n{len(canonical)}".encode()
    signature = bytearray(private_key.sign_msg_hash(keccak(prefix + canonical)).to_bytes())
    signature[64] += 27
    return {
        "payload": payload,
        "signature": {
            "algorithm": "secp256k1",
            "canonicalization": "jcs",
            "value": "0x" + signature.hex(),
        },
    }


def signed_session_settlement(
    *,
    gateway_session_id: str,
    session_id: str = "broker-session-1",
    work_id: str = "work-1",
    predecessor_work_id: str = "",
    rotation_generation: int = 0,
    debited_units: int = 31,
    claimed_units: int | None = None,
    generation_debited_units: int | None = None,
    billed_value_wei: int = 4,
    funded_value_wei: int = 100,
    generation_billed_value_wei: int | None = None,
    generation_funded_value_wei: int = 100,
    amount_wei: int = 100,
    per_units: int = 1000,
    settlement_seq: int = 1,
    state: str = "closed",
    work_unit: str = "token",
    issued_at: str = TEST_ISSUED_AT,
    outcome: str = "OVERFUNDED",
    private_key: PrivateKey = TEST_PRIVATE_KEY,
) -> dict[str, Any]:
    """Build one signed paid-session/v1 settlement envelope."""

    claimed = debited_units if claimed_units is None else claimed_units
    generation_units = (
        debited_units if generation_debited_units is None else generation_debited_units
    )
    generation_billed = (
        billed_value_wei if generation_billed_value_wei is None else generation_billed_value_wei
    )
    payload: dict[str, Any] = {
        "work_unit_name": work_unit,
        "actual_units": str(debited_units),
        "billed_units": str(debited_units),
        "funded_value_wei": {"value": base64.b64encode(_unsigned_bytes(funded_value_wei)).decode()},
        "billed_value_wei": {"value": base64.b64encode(_unsigned_bytes(billed_value_wei)).decode()},
        "outcome": outcome,
        "session_id": session_id,
        "gateway_session_id": gateway_session_id,
        "work_id": work_id,
        "predecessor_work_id": predecessor_work_id,
        "rotation_generation": rotation_generation,
        "claimed_units": str(claimed),
        "debited_units": str(debited_units),
        "generation_debited_units": str(generation_units),
        "generation_billed_value_wei": {
            "value": base64.b64encode(_unsigned_bytes(generation_billed)).decode()
        },
        "generation_funded_value_wei": {
            "value": base64.b64encode(_unsigned_bytes(generation_funded_value_wei)).decode()
        },
        "amount_wei": {"value": base64.b64encode(_unsigned_bytes(amount_wei)).decode()},
        "per_units": str(per_units),
        "settlement_seq": str(settlement_seq),
        "issued_at": issued_at,
        "state": state,
    }
    canonical = rfc8785.dumps(payload)
    prefix = f"\x19Ethereum Signed Message:\n{len(canonical)}".encode()
    signature = bytearray(private_key.sign_msg_hash(keccak(prefix + canonical)).to_bytes())
    signature[64] += 27
    return {
        "payload": payload,
        "signature": {
            "algorithm": "secp256k1",
            "canonicalization": "jcs",
            "value": "0x" + signature.hex(),
        },
    }


def _unsigned_bytes(value: int) -> bytes:
    if value == 0:
        return b""
    return value.to_bytes((value.bit_length() + 7) // 8, "big")
