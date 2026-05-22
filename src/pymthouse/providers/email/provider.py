"""EmailProvider Protocol and concrete implementations.

Always go through the Protocol; never call the Resend SDK directly from
domain code. In dev, `NullEmailProvider` logs the email body so the
verification token appears in stdout for testing.
"""

from __future__ import annotations

from typing import Protocol

import structlog

from pymthouse.providers.telemetry import get_logger
from pymthouse.settings import Settings

logger = get_logger(__name__)


class EmailMessage(Protocol):
    """The minimum email shape every provider must handle."""

    @property
    def to(self) -> str: ...
    @property
    def subject(self) -> str: ...
    @property
    def html(self) -> str: ...
    @property
    def text(self) -> str: ...


class _Message:
    """Concrete EmailMessage used by senders."""

    def __init__(self, *, to: str, subject: str, html: str, text: str) -> None:
        self.to = to
        self.subject = subject
        self.html = html
        self.text = text


class EmailProvider(Protocol):
    """An outbound transactional email sender."""

    async def send(self, message: EmailMessage) -> None: ...


class NullEmailProvider:
    """Logs the email body. Used in dev when no Resend key is configured."""

    def __init__(self, log: structlog.stdlib.BoundLogger | None = None) -> None:
        self._log = log or logger

    async def send(self, message: EmailMessage) -> None:
        self._log.info(
            "email.null.send",
            to=message.to,
            subject=message.subject,
            text=message.text,
        )


class ResendEmailProvider:
    """Sends mail via the Resend HTTP API.

    The Resend SDK is synchronous; we hop to a worker thread per call so
    the event loop stays responsive. Outcomes are emitted as
    ``email.resend.sent`` / ``email.resend.failed`` log lines.
    """

    def __init__(self, settings: Settings) -> None:
        # Import lazily so the dependency isn't required when NullEmail is used.
        import resend  # noqa: PLC0415

        if settings.resend_api_key is None:
            raise RuntimeError("ResendEmailProvider requires RESEND_API_KEY")
        resend.api_key = settings.resend_api_key.get_secret_value()
        self._resend = resend
        self._from = f"{settings.email_from_name} <{settings.email_from_address}>"
        self._log = logger

    async def send(self, message: EmailMessage) -> None:
        import asyncio  # noqa: PLC0415

        payload = {
            "from": self._from,
            "to": [message.to],
            "subject": message.subject,
            "html": message.html,
            "text": message.text,
        }
        try:
            result = await asyncio.to_thread(self._resend.Emails.send, payload)
        except Exception as exc:  # noqa: BLE001 — provider-level catch
            self._log.error(
                "email.resend.failed",
                to=message.to,
                subject=message.subject,
                error=str(exc),
            )
            raise
        provider_id = (
            result.get("id") if isinstance(result, dict) else None
        )
        self._log.info(
            "email.resend.sent",
            to=message.to,
            subject=message.subject,
            provider_id=provider_id,
        )


def make_message(*, to: str, subject: str, html: str, text: str) -> EmailMessage:
    """Constructor for the concrete EmailMessage."""
    return _Message(to=to, subject=subject, html=html, text=text)


def make_provider(settings: Settings) -> EmailProvider:
    """Pick an EmailProvider based on ``settings.email_provider``.

    ``auto`` (default) selects Resend when ``RESEND_API_KEY`` is set,
    Null otherwise. ``null`` and ``resend`` force the choice — useful in
    tests and for explicit prod configuration.
    """
    choice = settings.email_provider
    if choice == "null":
        return NullEmailProvider()
    if choice == "resend":
        return ResendEmailProvider(settings)
    # auto
    if settings.resend_api_key is not None:
        return ResendEmailProvider(settings)
    return NullEmailProvider()
