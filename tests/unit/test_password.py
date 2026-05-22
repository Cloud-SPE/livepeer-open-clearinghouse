"""Unit tests for password hashing."""

from __future__ import annotations

import pytest

from pymthouse.providers.auth import password


@pytest.mark.unit
def test_hash_round_trip() -> None:
    h = password.hash("correct horse battery staple")
    assert password.verify("correct horse battery staple", h) is True
    assert password.verify("wrong password", h) is False


@pytest.mark.unit
def test_hashes_are_salted() -> None:
    h1 = password.hash("same-input")
    h2 = password.hash("same-input")
    assert h1 != h2  # argon2 produces a fresh salt every time
    assert password.verify("same-input", h1)
    assert password.verify("same-input", h2)


@pytest.mark.unit
def test_verify_against_malformed_hash() -> None:
    assert password.verify("anything", "not-an-argon2-hash") is False
