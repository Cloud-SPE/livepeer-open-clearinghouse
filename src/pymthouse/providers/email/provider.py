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


class EmailSendError(RuntimeError):
    """Raised when a provider's send call reported a failure.

    Distinct from the SDK's own exception types because some providers
    (notably Resend's Python SDK against certain self-hosted backends)
    return an ``{error: "..."}`` body on a 2xx status instead of raising.
    Domain code catches this same exception regardless of provider.
    """


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

        if settings.resend_api_key is None or not settings.resend_api_key.get_secret_value():
            raise RuntimeError("ResendEmailProvider requires RESEND_API_KEY")
        resend.api_key = settings.resend_api_key.get_secret_value()
        # Optional override for the API base URL (regional Resend, on-prem
        # proxy, mocked endpoint in tests). The SDK exposes this as a
        # module-global string defaulting to https://api.resend.com.
        if settings.resend_api_url:
            resend.api_url = settings.resend_api_url
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
        # Pre-send log: enough to reproduce by curl. The SDK appends
        # /emails to the base URL; we surface that explicitly so a path-
        # prefix mismatch with a self-hosted Resend shows up clearly.
        target_url = f"{self._resend.api_url.rstrip('/')}/emails"
        html_preview = message.html[:80].replace("\n", " ")
        text_preview = message.text[:80].replace("\n", " ")
        self._log.info(
            "email.resend.sending",
            target_url=target_url,
            from_=self._from,
            to=message.to,
            subject=message.subject,
            html_bytes=len(message.html),
            text_bytes=len(message.text),
            html_preview=html_preview,
            text_preview=text_preview,
        )
        try:
            result = await asyncio.to_thread(self._resend.Emails.send, payload)
        except Exception as exc:  # noqa: BLE001 — provider-level catch
            # ResendError carries .code / .message / .suggested_action.
            # Generic exceptions fall through to repr() for shape clarity.
            self._log.error(
                "email.resend.failed",
                target_url=target_url,
                to=message.to,
                subject=message.subject,
                error_type=type(exc).__name__,
                error=str(exc),
                error_code=getattr(exc, "code", None),
                error_status=getattr(exc, "status_code", None),
                error_message=getattr(exc, "message", None),
                error_repr=repr(exc),
            )
            raise
        # Some Resend-compatible backends (notably self-hosted distributions)
        # return a 2xx status with an `{error: "..."}` body when the send
        # was actually rejected — the SDK doesn't raise on that shape, so
        # without this check we'd silently log "sent" for a dead letter.
        # The success shape always carries an `id` (or `message_id` /
        # `email_id` on some forks); the absence-of-id + presence-of-error
        # pattern is the failure signal.
        is_dict_result = isinstance(result, dict)
        provider_id = None
        if is_dict_result:
            provider_id = (
                result.get("id")
                or result.get("message_id")
                or result.get("email_id")
            )
        if is_dict_result and provider_id is None and result.get("error"):
            self._log.error(
                "email.resend.failed",
                target_url=target_url,
                to=message.to,
                subject=message.subject,
                error_type="ProviderRejected",
                error=str(result.get("error")),
                raw_result=result,
                raw_result_keys=sorted(result.keys()),
            )
            raise EmailSendError(
                f"Resend rejected the send: {result.get('error')!r}"
            )
        self._log.info(
            "email.resend.sent",
            target_url=target_url,
            to=message.to,
            subject=message.subject,
            provider_id=provider_id,
            raw_result=result if is_dict_result else repr(result),
            raw_result_keys=sorted(result.keys()) if is_dict_result else None,
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
