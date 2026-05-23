"""Transactional email — EmailProvider Protocol + Resend impl + NullEmail fallback."""

from livepeer_open_clearinghouse.providers.email import templates
from livepeer_open_clearinghouse.providers.email.provider import (
    EmailMessage,
    EmailProvider,
    NullEmailProvider,
    ResendEmailProvider,
    make_message,
    make_provider,
)

__all__ = [
    "EmailMessage",
    "EmailProvider",
    "NullEmailProvider",
    "ResendEmailProvider",
    "make_message",
    "make_provider",
    "templates",
]
