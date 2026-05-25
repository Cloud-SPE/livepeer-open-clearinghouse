"""Ed25519 signing for the operator-published SDK manifest.

The signing key is a 32-byte Ed25519 seed read from settings. SDKs
fetch the public key at ``/v1/sdk/manifest/pubkey`` and verify the
``signature`` field on each manifest payload before trusting it.

Verification is offline — once an SDK has cached the operator's
public key, future manifest fetches don't require it to re-trust LOC.
This is the security model from exec-plan 002 §"SDK approval-list":
the manifest is operator-signed; LOC just serves it.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

_ED25519_SEED_LEN = 32
_ED25519_PUB_LEN = 32
_ED25519_SIG_LEN = 64


@dataclass(frozen=True)
class SigningKeypair:
    """The decoded operator signing keypair."""

    private: Ed25519PrivateKey
    public: Ed25519PublicKey
    public_bytes: bytes  # raw 32 bytes
    fingerprint: str  # first 16 hex chars of SHA-256(public_bytes)


class SigningKeyError(Exception):
    """The configured seed couldn't be decoded into a usable key."""


def load_keypair(seed_b64: str) -> SigningKeypair:
    """Decode a base64 32-byte seed into a usable Ed25519 keypair."""
    try:
        seed = base64.b64decode(seed_b64, validate=True)
    except (ValueError, base64.binascii.Error) as exc:  # type: ignore[attr-defined]
        raise SigningKeyError(f"sdk_manifest_signing_key not valid base64: {exc}") from exc
    if len(seed) != _ED25519_SEED_LEN:
        raise SigningKeyError(
            f"sdk_manifest_signing_key must decode to {_ED25519_SEED_LEN} bytes, got {len(seed)}"
        )
    private = Ed25519PrivateKey.from_private_bytes(seed)
    public = private.public_key()
    from cryptography.hazmat.primitives import serialization  # noqa: PLC0415

    public_bytes = public.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    fp = hashlib.sha256(public_bytes).hexdigest()[:16]
    return SigningKeypair(
        private=private,
        public=public,
        public_bytes=public_bytes,
        fingerprint=fp,
    )


def sign_payload(keypair: SigningKeypair, payload: dict[str, Any]) -> str:
    """Sign the canonical JSON of ``payload``. Returns base64 (no
    padding) of the 64-byte Ed25519 signature.

    Canonical = ``json.dumps(payload, sort_keys=True, separators=(",", ":"))``
    so the verifier gets the same bytes regardless of dict ordering.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    sig = keypair.private.sign(canonical)
    return base64.urlsafe_b64encode(sig).rstrip(b"=").decode("ascii")


def verify_payload(
    public_b64: str,
    *,
    payload: dict[str, Any],
    signature_b64: str,
) -> bool:
    """Reverse of :func:`sign_payload`. Used by SDK-side verification
    (and by tests). Returns True iff the signature checks out.

    Both inputs are URL-safe base64 with optional trailing-padding
    stripping.
    """
    try:
        pub_raw = base64.urlsafe_b64decode(public_b64 + "=" * (-len(public_b64) % 4))
        sig_raw = base64.urlsafe_b64decode(signature_b64 + "=" * (-len(signature_b64) % 4))
    except (ValueError, base64.binascii.Error):  # type: ignore[attr-defined]
        return False
    if len(pub_raw) != _ED25519_PUB_LEN or len(sig_raw) != _ED25519_SIG_LEN:
        return False
    public = Ed25519PublicKey.from_public_bytes(pub_raw)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    try:
        public.verify(sig_raw, canonical)
    except InvalidSignature:
        return False
    return True


def public_key_b64(keypair: SigningKeypair) -> str:
    """Base64 of the raw 32-byte public key. Served by
    ``/v1/sdk/manifest/pubkey``."""
    return base64.urlsafe_b64encode(keypair.public_bytes).rstrip(b"=").decode("ascii")
