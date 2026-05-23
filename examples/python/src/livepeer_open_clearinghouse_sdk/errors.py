"""Typed exceptions mapped from Livepeer Open Clearinghouse's error envelope.

Livepeer Open Clearinghouse returns errors as:

    {"error": {"code": "...", "message": "...", "details": {...}}}

This module turns those into Python exception classes the caller can
match on. Anything we don't recognize falls through to the base
OpenClearinghouseError so callers can still log + retry sensibly.
"""

from __future__ import annotations

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


def from_response(
    *, status: int, body: dict[str, Any], retry_after: int | None
) -> OpenClearinghouseError:
    """Build the right exception subclass from a parsed JSON error body."""
    envelope = body.get("error") or {}
    code = envelope.get("code") or body.get("detail")
    message = envelope.get("message") or body.get("detail") or f"HTTP {status}"
    details = envelope.get("details") or {}
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
