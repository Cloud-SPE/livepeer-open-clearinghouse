"""Transactional email — EmailProvider Protocol + Resend impl + NullEmail fallback."""

from pymthouse.providers.email import templates
from pymthouse.providers.email.provider import (
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
