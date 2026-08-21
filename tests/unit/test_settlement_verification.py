from __future__ import annotations

import copy

import pytest

from livepeer_open_clearinghouse.providers.settlement_verification import (
    JobSettlementExpectation,
    SessionSettlementExpectation,
    SettlementVerificationError,
    verify_job_settlement,
    verify_session_settlement,
)
from tests.fixtures.signed_settlement import (
    TEST_PUBLIC_KEY,
    delegated_key,
    signed_job_settlement,
    signed_session_settlement,
)


def _expected(**overrides: object) -> JobSettlementExpectation:
    values = {
        "job_id": "job-1",
        "work_id": "work-1",
        "work_unit": "token",
        "actual_units": 31,
        "amount_wei": 100,
        "per_units": 1000,
        "quote_id": "q-1",
        "quote_version": 1,
        "constraint_fingerprint": b"\x00" * 32,
        "route_fingerprint": b"\x11" * 32,
    }
    values.update(overrides)
    return JobSettlementExpectation(**values)  # type: ignore[arg-type]


def _envelope(**overrides: object) -> dict[str, object]:
    values = {
        "job_id": "job-1",
        "work_id": "work-1",
        "actual_units": 31,
        "amount_wei": 100,
        "per_units": 1000,
    }
    values.update(overrides)
    return signed_job_settlement(**values)  # type: ignore[arg-type]


@pytest.mark.unit
def test_valid_job_settlement_verifies_ceiling_and_overlap_key() -> None:
    older = delegated_key(public_key="0x04" + "22" * 64)
    verified = verify_job_settlement(
        _envelope(), settlement_keys=[older, delegated_key()], expected=_expected()
    )
    assert verified.actual_units == 31
    assert verified.billed_value_wei == 4
    assert verified.signing_public_key == TEST_PUBLIC_KEY


@pytest.mark.unit
def test_job_settlement_uses_shared_payment_cumulative_curve() -> None:
    verified = verify_job_settlement(
        _envelope(actual_units=42, payment_cumulative_units=84),
        settlement_keys=[delegated_key()],
        expected=_expected(actual_units=42),
    )
    assert verified.billed_value_wei == 4


@pytest.mark.unit
def test_job_settlement_rejects_impossible_payment_curve() -> None:
    with pytest.raises(SettlementVerificationError) as exc_info:
        verify_job_settlement(
            _envelope(payment_cumulative_units=30),
            settlement_keys=[delegated_key()],
            expected=_expected(),
        )
    assert exc_info.value.code == "payment_curve_invalid"


@pytest.mark.unit
def test_tampered_payload_fails_signature() -> None:
    envelope = _envelope()
    envelope["payload"]["actual_units"] = "32"  # type: ignore[index]
    with pytest.raises(SettlementVerificationError) as exc_info:
        verify_job_settlement(envelope, settlement_keys=[delegated_key()], expected=_expected())
    assert exc_info.value.code == "unauthorized_signing_key"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("expected_override", "code"),
    [
        ({"job_id": "other"}, "job_id_mismatch"),
        ({"work_id": "other"}, "work_id_mismatch"),
        ({"work_unit": "frame"}, "work_unit_mismatch"),
        ({"actual_units": 30}, "work_units_mismatch"),
        ({"quote_id": "other"}, "quote_mismatch"),
        ({"amount_wei": 200}, "billed_value_mismatch"),
    ],
)
def test_signed_claim_must_match_pinned_job(
    expected_override: dict[str, object], code: str
) -> None:
    with pytest.raises(SettlementVerificationError) as exc_info:
        verify_job_settlement(
            _envelope(),
            settlement_keys=[delegated_key()],
            expected=_expected(**expected_override),
        )
    assert exc_info.value.code == code


@pytest.mark.unit
def test_key_must_be_delegated_at_signed_time() -> None:
    with pytest.raises(SettlementVerificationError) as exc_info:
        verify_job_settlement(
            _envelope(),
            settlement_keys=[
                delegated_key(
                    not_before="2026-08-20T13:00:00Z",
                    expires_at="2026-08-21T00:00:00Z",
                )
            ],
            expected=_expected(),
        )
    assert exc_info.value.code == "unauthorized_signing_key"


@pytest.mark.unit
@pytest.mark.parametrize(
    "envelope",
    [
        {},
        {"payload": {}, "signature": {}},
        {
            "payload": {},
            "signature": {"algorithm": "secp256k1", "canonicalization": "jcs", "value": "nope"},
        },
    ],
)
def test_unsigned_and_malformed_envelopes_fail_closed(envelope: dict[str, object]) -> None:
    with pytest.raises(SettlementVerificationError):
        verify_job_settlement(envelope, settlement_keys=[delegated_key()], expected=_expected())


@pytest.mark.unit
@pytest.mark.parametrize(
    "keys",
    [
        [delegated_key(public_key="0x04" + "22" * 64)],
        [delegated_key(expires_at="2026-08-20T11:59:59Z")],
    ],
)
def test_unknown_and_expired_keys_fail_closed(keys: list[dict[str, object]]) -> None:
    with pytest.raises(SettlementVerificationError) as exc_info:
        verify_job_settlement(_envelope(), settlement_keys=keys, expected=_expected())
    assert exc_info.value.code == "unauthorized_signing_key"


@pytest.mark.unit
def test_cross_job_replay_fails_even_when_every_other_field_matches() -> None:
    replay = copy.deepcopy(_envelope())
    with pytest.raises(SettlementVerificationError) as exc_info:
        verify_job_settlement(
            replay,
            settlement_keys=[delegated_key()],
            expected=_expected(job_id="job-2"),
        )
    assert exc_info.value.code == "job_id_mismatch"


def _session_expected(**overrides: object) -> SessionSettlementExpectation:
    values = {
        "gateway_session_id": "11111111-1111-1111-1111-111111111111",
        "broker_session_id": None,
        "work_id": "work-1",
        "predecessor_work_id": "",
        "rotation_generation": 0,
        "work_unit": "token",
        "amount_wei": 100,
        "per_units": 1000,
        "funded_value_wei": 100,
        "last_settlement_seq": 0,
    }
    values.update(overrides)
    return SessionSettlementExpectation(**values)  # type: ignore[arg-type]


@pytest.mark.unit
def test_valid_session_settlement_binds_gateway_identity_and_signed_charge() -> None:
    envelope = signed_session_settlement(gateway_session_id="11111111-1111-1111-1111-111111111111")
    verified = verify_session_settlement(
        envelope,
        settlement_keys=[delegated_key()],
        expected=_session_expected(),
    )
    assert verified.broker_session_id == "broker-session-1"
    assert verified.debited_units == 31
    assert verified.billed_value_wei == 4
    assert verified.settlement_seq == 1


@pytest.mark.unit
@pytest.mark.parametrize(
    ("envelope_override", "expected_override", "code"),
    [
        ({"gateway_session_id": "other"}, {}, "gateway_session_id_mismatch"),
        ({"session_id": "fork"}, {"broker_session_id": "known"}, "session_id_mismatch"),
        ({"work_id": "other"}, {}, "work_id_mismatch"),
        ({"predecessor_work_id": "other"}, {}, "predecessor_mismatch"),
        ({"rotation_generation": 1}, {}, "rotation_generation_mismatch"),
        ({"work_unit": "frame"}, {}, "work_unit_mismatch"),
        ({"amount_wei": 101}, {}, "pricing_mismatch"),
        ({"settlement_seq": 1}, {"last_settlement_seq": 1}, "settlement_replay"),
        ({"state": "open"}, {}, "session_not_terminal"),
        ({"claimed_units": 32}, {}, "claim_debit_gap"),
        ({"generation_debited_units": 32}, {}, "generation_units_invalid"),
        ({"billed_value_wei": 101}, {}, "session_cap_exceeded"),
        ({"generation_billed_value_wei": 5}, {}, "generation_value_invalid"),
    ],
)
def test_session_settlement_mismatches_fail_closed(
    envelope_override: dict[str, object],
    expected_override: dict[str, object],
    code: str,
) -> None:
    envelope_args: dict[str, object] = {
        "gateway_session_id": "11111111-1111-1111-1111-111111111111"
    }
    envelope_args.update(envelope_override)
    envelope = signed_session_settlement(**envelope_args)  # type: ignore[arg-type]
    with pytest.raises(SettlementVerificationError) as exc_info:
        verify_session_settlement(
            envelope,
            settlement_keys=[delegated_key()],
            expected=_session_expected(**expected_override),
        )
    assert exc_info.value.code == code


@pytest.mark.unit
def test_rotated_session_requires_exact_generation_chain_tip() -> None:
    envelope = signed_session_settlement(
        gateway_session_id="11111111-1111-1111-1111-111111111111",
        work_id="work-2",
        predecessor_work_id="work-1",
        rotation_generation=1,
    )
    verified = verify_session_settlement(
        envelope,
        settlement_keys=[delegated_key()],
        expected=_session_expected(
            work_id="work-2",
            predecessor_work_id="work-1",
            rotation_generation=1,
        ),
    )
    assert verified.predecessor_work_id == "work-1"
    assert verified.rotation_generation == 1
