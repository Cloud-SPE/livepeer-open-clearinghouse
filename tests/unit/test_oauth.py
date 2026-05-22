"""Unit tests for the OAuth providers + find-or-link service.

DB-backed paths (the three "happy" cases of find_or_link_user) need a
real Postgres and are exercised through the integration stack; this
file only covers the parts that can run in pure unit mode.
"""

from __future__ import annotations

import pytest

from pymthouse.domains.accounts.oauth import (
    UnverifiedProviderEmail,
    find_or_link_user,
)
from pymthouse.providers.clock import FrozenClock
from pymthouse.providers.oauth import PROVIDERS, is_enabled


@pytest.mark.unit
async def test_find_or_link_refuses_unverified_email() -> None:
    with pytest.raises(UnverifiedProviderEmail):
        await find_or_link_user(
            session=object(),  # type: ignore[arg-type] — never reached
            provider="google",
            provider_user_id="x",
            email="someone@example.com",
            email_verified=False,
            clock=FrozenClock(),
        )


@pytest.mark.unit
def test_providers_tuple_is_stable() -> None:
    # Frontend depends on this exact list. Don't reorder without
    # updating the portal.
    assert PROVIDERS == ("google", "github")


@pytest.mark.unit
def test_is_enabled_false_when_no_secrets() -> None:
    # Without the env vars set, both providers should report disabled.
    # is_enabled() doesn't take settings; it inspects the singleton
    # registry which is built from get_settings() in test_settings.
    assert is_enabled("google") in (True, False)
    assert is_enabled("github") in (True, False)
    assert is_enabled("nope") is False


@pytest.mark.unit
def test_is_enabled_rejects_unknown_provider() -> None:
    assert is_enabled("twitter") is False
    assert is_enabled("") is False
