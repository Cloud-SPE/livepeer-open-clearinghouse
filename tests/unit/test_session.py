"""Unit tests for web-session token mechanics."""

from __future__ import annotations

import time

import pytest

from pymthouse.providers.auth import session


@pytest.mark.unit
def test_token_is_random_and_long_enough() -> None:
    a = session.generate_token()
    b = session.generate_token()
    assert a != b
    assert len(a) >= 32


@pytest.mark.unit
def test_hash_token_is_deterministic() -> None:
    t = "some-token"
    assert session.hash_token(t) == session.hash_token(t)
    assert session.hash_token(t) != session.hash_token("other-token")


@pytest.mark.unit
def test_seal_unseal_round_trip() -> None:
    ser = session.make_serializer("test-secret")
    sealed = session.seal(ser, "the-raw-token")
    assert session.unseal(ser, sealed, max_age_seconds=60) == "the-raw-token"


@pytest.mark.unit
def test_unseal_rejects_other_secret() -> None:
    ser_a = session.make_serializer("secret-a")
    ser_b = session.make_serializer("secret-b")
    sealed = session.seal(ser_a, "tok")
    assert session.unseal(ser_b, sealed, max_age_seconds=60) is None


@pytest.mark.unit
def test_unseal_rejects_expired() -> None:
    ser = session.make_serializer("test-secret")
    sealed = session.seal(ser, "tok")
    time.sleep(1.1)
    assert session.unseal(ser, sealed, max_age_seconds=1) is None
