"""Centralized email templates.

Pure builders — no I/O. Callers compose an `EmailMessage` here and hand
it to whatever `EmailProvider` is configured. Keeping every template in
this one file makes it easy to audit what users actually receive.

When you add a template:
    1. Write a function returning ``EmailMessage`` via ``make_message``.
    2. Add a unit test covering subject + text + html.
"""

from __future__ import annotations

import html as html_lib

from livepeer_open_clearinghouse.providers.email.provider import EmailMessage, make_message

BRAND = "Livepeer Open Clearinghouse"


def _portal_link(base_url: str, path: str = "") -> str:
    return f"{base_url.rstrip('/')}/portal/{path.lstrip('/')}"


def _safe(text: str) -> str:
    """HTML-escape user-supplied text we splice into a template."""
    return html_lib.escape(text, quote=True)


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def verification_email(*, to: str, verify_link: str) -> EmailMessage:
    """The signup confirmation email — single-use link to consume the token."""
    subject = f"Verify your {BRAND} account"
    safe_link = _safe(verify_link)
    text = (
        f"Welcome to {BRAND}.\n\n"
        f"Click this link to verify your email:\n{verify_link}\n\n"
        f"The link expires in 24 hours. After verification an operator will "
        f"review and approve your account.\n"
    )
    html = (
        f"<p>Welcome to {BRAND}.</p>"
        f'<p><a href="{safe_link}">Click here to verify your email</a>.</p>'
        f"<p>The link expires in 24 hours. After verification an operator "
        f"will review and approve your account.</p>"
    )
    return make_message(to=to, subject=subject, html=html, text=text)


# ---------------------------------------------------------------------------
# Password reset
# ---------------------------------------------------------------------------


def password_reset_email(*, to: str, reset_link: str, ttl_minutes: int = 60) -> EmailMessage:
    """A password-reset link. Mention the TTL so the user knows it's time-bound."""
    subject = f"Reset your {BRAND} password"
    safe_link = _safe(reset_link)
    text = (
        f"A password reset was requested for your {BRAND} account.\n\n"
        f"Use this link to set a new password (expires in "
        f"{ttl_minutes} minutes):\n{reset_link}\n\n"
        f"If you didn't ask for this, you can ignore the email — your "
        f"password will not change.\n"
    )
    html = (
        f"<p>A password reset was requested for your {BRAND} account.</p>"
        f'<p><a href="{safe_link}">Click here to set a new password</a> '
        f"(expires in {ttl_minutes} minutes).</p>"
        f"<p>If you didn't ask for this, you can ignore this email — your "
        f"password will not change.</p>"
    )
    return make_message(to=to, subject=subject, html=html, text=text)


# ---------------------------------------------------------------------------
# Operator approval
# ---------------------------------------------------------------------------


def approval_notification_email(*, to: str, public_base_url: str) -> EmailMessage:
    """Sent once an operator approves a verified user."""
    subject = f"Your {BRAND} account is approved"
    portal = _portal_link(public_base_url)
    safe_portal = _safe(portal)
    text = (
        f"Your {BRAND} account has been approved.\n\n"
        f"You can now log in and create API keys at:\n{portal}\n\n"
        f"Each API key is shown to you only once at creation time — copy it "
        f"and store it securely.\n"
    )
    html = (
        f"<p>Your {BRAND} account has been approved.</p>"
        f'<p><a href="{safe_portal}">Log in to the portal</a> to create your '
        f"API keys.</p>"
        f"<p>Each API key is shown to you only once at creation time — copy "
        f"it and store it securely.</p>"
    )
    return make_message(to=to, subject=subject, html=html, text=text)
