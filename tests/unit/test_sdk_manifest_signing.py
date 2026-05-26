"""Tests for the operator manifest signing flow."""

from __future__ import annotations

import base64
import secrets

import pytest

from livepeer_open_clearinghouse.providers.signing.manifest import (
    SigningKeyError,
    load_keypair,
    public_key_b64,
    sign_payload,
    verify_payload,
)


def _make_seed_b64() -> str:
    return base64.b64encode(secrets.token_bytes(32)).decode("ascii")


@pytest.mark.unit
def test_load_keypair_decodes_seed() -> None:
    kp = load_keypair(_make_seed_b64())
    assert len(kp.public_bytes) == 32
    assert len(kp.fingerprint) == 16  # first 16 hex chars of SHA-256


@pytest.mark.unit
def test_load_keypair_rejects_wrong_length() -> None:
    too_short = base64.b64encode(b"\x00" * 16).decode("ascii")
    with pytest.raises(SigningKeyError):
        load_keypair(too_short)


@pytest.mark.unit
def test_load_keypair_rejects_non_base64() -> None:
    with pytest.raises(SigningKeyError):
        load_keypair("not!!base64@@")


@pytest.mark.unit
def test_sign_then_verify_roundtrip() -> None:
    kp = load_keypair(_make_seed_b64())
    payload = {
        "items": [{"lang": "py", "version": "0.2.0", "git_sha7": "abc1234", "status": "approved"}],
        "generated_at": "2026-05-25T12:00:00+00:00",
    }
    sig = sign_payload(kp, payload)
    pub_b64 = public_key_b64(kp)
    assert verify_payload(pub_b64, payload=payload, signature_b64=sig) is True


@pytest.mark.unit
def test_verify_fails_on_payload_tamper() -> None:
    kp = load_keypair(_make_seed_b64())
    payload = {"items": [], "generated_at": "2026-05-25T12:00:00+00:00"}
    sig = sign_payload(kp, payload)
    pub_b64 = public_key_b64(kp)
    tampered = {
        "items": [{"lang": "py", "version": "9.9.9", "git_sha7": "evil123", "status": "approved"}],
        "generated_at": payload["generated_at"],
    }
    assert verify_payload(pub_b64, payload=tampered, signature_b64=sig) is False


@pytest.mark.unit
def test_verify_fails_on_signature_tamper() -> None:
    kp = load_keypair(_make_seed_b64())
    payload = {"items": [], "generated_at": "2026-05-25T12:00:00+00:00"}
    sig = sign_payload(kp, payload)
    # Flip one base64 char.
    tampered_sig = ("a" if sig[0] != "a" else "b") + sig[1:]
    pub_b64 = public_key_b64(kp)
    assert verify_payload(pub_b64, payload=payload, signature_b64=tampered_sig) is False


@pytest.mark.unit
def test_verify_fails_on_wrong_key() -> None:
    kp1 = load_keypair(_make_seed_b64())
    kp2 = load_keypair(_make_seed_b64())
    payload = {"items": [], "generated_at": "2026-05-25T12:00:00+00:00"}
    sig = sign_payload(kp1, payload)
    # Verify under the *other* keypair's public key.
    assert verify_payload(public_key_b64(kp2), payload=payload, signature_b64=sig) is False


@pytest.mark.unit
def test_canonical_serialization_is_dict_order_stable() -> None:
    """Re-ordering keys at the source must not change the signature."""
    kp = load_keypair(_make_seed_b64())
    payload_a = {"a": 1, "b": 2, "c": 3}
    payload_b = {"c": 3, "a": 1, "b": 2}
    assert sign_payload(kp, payload_a) == sign_payload(kp, payload_b)
