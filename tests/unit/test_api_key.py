"""Unit tests for api_key credential mechanics."""

from __future__ import annotations

import pytest

from pymthouse.providers.auth import api_key


@pytest.mark.unit
def test_generated_key_has_expected_shape() -> None:
    key = api_key.generate()
    assert key.startswith(api_key.KEY_BRAND)
    assert len(key) > len(api_key.KEY_BRAND) + 16
    assert api_key.looks_well_formed(key)


@pytest.mark.unit
def test_prefix_is_stable_and_long_enough() -> None:
    key = "pymth_live_abcdefghijklmnop"
    pfx = api_key.prefix(key)
    assert pfx == "pymth_live_abcdefgh"
    assert len(pfx) == api_key.KEY_PREFIX_LEN


@pytest.mark.unit
def test_hash_is_deterministic_and_includes_pepper() -> None:
    raw = "pymth_live_abcdef1234567890"
    h1 = api_key.hash(raw, "pepper-a")
    h2 = api_key.hash(raw, "pepper-a")
    h3 = api_key.hash(raw, "pepper-b")
    assert h1 == h2
    assert h1 != h3


@pytest.mark.unit
def test_verify_matches_only_with_correct_pepper() -> None:
    raw = api_key.generate()
    stored = api_key.hash(raw, "pepper-a")
    assert api_key.verify(raw, stored, "pepper-a") is True
    assert api_key.verify(raw, stored, "pepper-b") is False
    assert api_key.verify("pymth_live_wrong-key-here", stored, "pepper-a") is False


@pytest.mark.unit
def test_looks_well_formed_rejects_garbage() -> None:
    assert api_key.looks_well_formed("") is False
    assert api_key.looks_well_formed("garbage") is False
    assert api_key.looks_well_formed("pymth_live_") is False
    assert api_key.looks_well_formed("pymth_live_abcdef12345") is True
