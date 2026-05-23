"""Unit tests for ResendEmailProvider.send() outcome detection.

The Python Resend SDK against certain self-hosted backends returns a
2xx status with ``{error: "..."}`` instead of raising. We need to treat
that body as a failure rather than silently logging "sent" with
``provider_id=None``. These tests stub the SDK's ``Emails.send``
classmethod and verify the branch the provider takes for each shape.
"""

from __future__ import annotations

import types

import pytest

from pymthouse.providers.email.provider import (
    EmailSendError,
    ResendEmailProvider,
    make_message,
)
from pymthouse.settings import Settings


def _settings() -> Settings:
    return Settings(
        app_env="dev",
        database_url="postgresql+asyncpg://x:x@localhost/x",
        admin_bootstrap_token=None,
        email_provider="resend",
        resend_api_key="re_test_fake_key",
        email_from_address="no-reply@example.com",
        email_from_name="Test",
    )


def _build_provider(monkeypatch: pytest.MonkeyPatch, send_result: object):
    """Stub the SDK's send to return ``send_result`` (or raise if it's an Exception)."""
    provider = ResendEmailProvider(_settings())

    class _Emails:
        @staticmethod
        def send(_payload: dict) -> object:
            if isinstance(send_result, Exception):
                raise send_result
            return send_result

    provider._resend = types.SimpleNamespace(  # type: ignore[attr-defined]
        Emails=_Emails,
        api_url="https://api.resend.com",
    )
    return provider


@pytest.mark.unit
async def test_success_shape_returns_quietly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _build_provider(
        monkeypatch, {"id": "abc-123", "from": "x", "to": ["y"]}
    )
    msg = make_message(to="user@example.com", subject="s", html="h", text="t")
    await provider.send(msg)  # no exception


@pytest.mark.unit
async def test_alternate_id_keys_count_as_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Some self-hosted backends use {message_id: ...} or {email_id: ...}.
    for shape in (
        {"message_id": "abc-123"},
        {"email_id": "abc-123"},
    ):
        provider = _build_provider(monkeypatch, shape)
        msg = make_message(to="user@example.com", subject="s", html="h", text="t")
        await provider.send(msg)


@pytest.mark.unit
async def test_error_body_without_id_raises_EmailSendError(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The exact shape we hit against the user's self-hosted Resend:
    # 2xx with `{"error": "From email must be from domain: ..."}` and
    # no `id`. This was previously silently logged as success.
    provider = _build_provider(
        monkeypatch,
        {"error": "From email must be from domain: mail.example.com"},
    )
    msg = make_message(to="user@example.com", subject="s", html="h", text="t")
    with pytest.raises(EmailSendError, match="From email must be from domain"):
        await provider.send(msg)


@pytest.mark.unit
async def test_sdk_exception_still_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the SDK itself raises, we re-raise after logging — unchanged behavior."""
    provider = _build_provider(monkeypatch, RuntimeError("boom"))
    msg = make_message(to="user@example.com", subject="s", html="h", text="t")
    with pytest.raises(RuntimeError, match="boom"):
        await provider.send(msg)
