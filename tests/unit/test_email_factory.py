"""Unit tests for the EmailProvider factory selection logic."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from pymthouse.providers.email import (
    NullEmailProvider,
    make_provider,
)
from pymthouse.settings import Settings


def _s(**overrides: object) -> Settings:
    return Settings(
        app_env="dev",
        database_url="postgresql+asyncpg://x:x@localhost/x",
        **overrides,  # type: ignore[arg-type]
    )


@pytest.mark.unit
def test_auto_with_no_key_uses_null() -> None:
    p = make_provider(_s(email_provider="auto", resend_api_key=None))
    assert isinstance(p, NullEmailProvider)


@pytest.mark.unit
def test_explicit_null_overrides_key() -> None:
    p = make_provider(
        _s(email_provider="null", resend_api_key=SecretStr("rs_secret"))
    )
    assert isinstance(p, NullEmailProvider)


@pytest.mark.unit
def test_explicit_resend_without_key_raises() -> None:
    with pytest.raises(RuntimeError, match="RESEND_API_KEY"):
        make_provider(_s(email_provider="resend", resend_api_key=None))


@pytest.mark.unit
def test_auto_with_key_picks_resend() -> None:
    # If the `resend` import is available, this should construct ok.
    pytest.importorskip("resend")
    p = make_provider(
        _s(email_provider="auto", resend_api_key=SecretStr("rs_secret"))
    )
    # ResendEmailProvider has no isinstance check against NullEmailProvider.
    assert not isinstance(p, NullEmailProvider)
