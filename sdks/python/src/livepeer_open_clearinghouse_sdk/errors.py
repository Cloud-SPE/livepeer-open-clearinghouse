"""Typed exceptions mapped from Livepeer Open Clearinghouse's error envelope.

Livepeer Open Clearinghouse returns errors as:

    {"error": {"code": "...", "message": "...", "details": {...}}}

This module turns those into Python exception classes the caller can
match on. Anything we don't recognize falls through to the base
OpenClearinghouseError so callers can still log + retry sensibly.
"""

from __future__ import annotations

import json
from typing import Any


class OpenClearinghouseError(RuntimeError):
    """Base for any error the gateway returns. Carries the wire envelope."""

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = details or {}


class InsufficientCredit(OpenClearinghouseError):
    """402 — user balance < required wei. Top up or wait for auto-replenish."""


class SpendCapExceeded(OpenClearinghouseError):
    """402 — per-period spend cap reached. Wait for the next window."""


class AccountNotApproved(OpenClearinghouseError):
    """403 — user signed up but operator hasn't approved them yet."""


class EmailNotVerified(OpenClearinghouseError):
    """403 — user hasn't completed email verification."""


class NoRouteAvailable(OpenClearinghouseError):
    """404 — no orch is currently advertising the requested capability."""


class RateLimited(OpenClearinghouseError):
    """429 — too many requests. Honor the Retry-After hint if present."""

    def __init__(
        self,
        message: str,
        *,
        retry_after_seconds: int | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, **kwargs)
        self.retry_after_seconds = retry_after_seconds


class DuplicateRequest(OpenClearinghouseError):
    """409 — same Idempotency-Key seen with different inputs."""


class DaemonUnavailable(OpenClearinghouseError):
    """502/503 — payment-daemon or registry-daemon unreachable."""


class BrokerProtocolError(OpenClearinghouseError):
    """The broker returned a response that violates the paid protocol contract."""


_CODE_MAP: dict[str, type[OpenClearinghouseError]] = {
    "INSUFFICIENT_CREDIT": InsufficientCredit,
    "SPEND_CAP_EXCEEDED": SpendCapExceeded,
    "ACCOUNT_NOT_APPROVED": AccountNotApproved,
    "account_not_approved": AccountNotApproved,
    "email_not_verified": EmailNotVerified,
    "NO_ROUTE_AVAILABLE": NoRouteAvailable,
    "rate_limited": RateLimited,
    "DUPLICATE_REQUEST": DuplicateRequest,
    "DAEMON_UNAVAILABLE": DaemonUnavailable,
}


_DETAIL_MESSAGE_MAX_CHARS = 500


def _compact_detail(detail: Any) -> str:
    """Render a non-string ``detail`` (FastAPI validation list) as one line."""
    try:
        text = json.dumps(detail, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        text = str(detail)
    if len(text) > _DETAIL_MESSAGE_MAX_CHARS:
        text = text[: _DETAIL_MESSAGE_MAX_CHARS - 3] + "..."
    return text


def from_response(
    *, status: int, body: dict[str, Any], retry_after: int | None
) -> OpenClearinghouseError:
    """Build the right exception subclass from a parsed JSON error body."""
    envelope = body.get("error") or {}
    if not isinstance(envelope, dict):
        envelope = {}
    detail = body.get("detail")
    # FastAPI validation errors carry ``detail`` as a list of objects.
    # Only a string detail can act as a code/message; anything else is
    # surfaced verbatim under ``details["detail"]`` so nothing is lost
    # and nothing here can raise.
    detail_str = detail if isinstance(detail, str) else None
    code = envelope.get("code") or detail_str
    if not isinstance(code, str):
        code = None
    message = envelope.get("message") or detail_str
    if not isinstance(message, str):
        message = _compact_detail(detail) if detail is not None else f"HTTP {status}"
    details = envelope.get("details") or {}
    if not isinstance(details, dict):
        details = {"details": details}
    if detail is not None and detail_str is None:
        details = {**details, "detail": detail}
    cls = _CODE_MAP.get(code or "", OpenClearinghouseError)
    if cls is RateLimited:
        return RateLimited(
            message,
            code=code,
            status=status,
            details=details,
            retry_after_seconds=retry_after,
        )
    return cls(message, code=code, status=status, details=details)
