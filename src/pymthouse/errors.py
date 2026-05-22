"""Structured PymtHouseError + FastAPI handler.

Use ``PymtHouseError`` (or its subclasses) inside services when you want a
machine-readable error code in the response. The handler emits the envelope
defined in ``docs/RELIABILITY.md``:

    {
        "error": {
            "code": "INSUFFICIENT_CREDIT",
            "message": "...",
            "details": { ... }
        }
    }

`HTTPException` is still fine for boring 401/404/422 shapes; use this when
the caller needs to act on the code.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class PymtHouseError(Exception):
    """Base class for errors that should be rendered with a structured envelope."""

    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}


class InsufficientCredit(PymtHouseError):
    def __init__(self, *, available_wei: int, required_wei: int) -> None:
        super().__init__(
            status_code=402,
            code="INSUFFICIENT_CREDIT",
            message=(
                f"Available balance {available_wei} wei is less than "
                f"required {required_wei} wei"
            ),
            details={
                "available_wei": str(available_wei),
                "required_wei": str(required_wei),
            },
        )


class SpendCapExceeded(PymtHouseError):
    def __init__(self, *, cap_wei: int, would_be_spent_wei: int) -> None:
        super().__init__(
            status_code=402,
            code="SPEND_CAP_EXCEEDED",
            message=(
                f"Window spend cap {cap_wei} wei would be exceeded "
                f"({would_be_spent_wei} wei after this charge)"
            ),
            details={
                "cap_wei": str(cap_wei),
                "would_be_spent_wei": str(would_be_spent_wei),
            },
        )


class AccountNotApproved(PymtHouseError):
    def __init__(self) -> None:
        super().__init__(
            status_code=403,
            code="ACCOUNT_NOT_APPROVED",
            message="Account has not been approved by an operator yet",
        )


class NoRouteAvailable(PymtHouseError):
    def __init__(self, *, capability: str, offering: str) -> None:
        super().__init__(
            status_code=404,
            code="NO_ROUTE_AVAILABLE",
            message=f"No route for capability={capability!r}, offering={offering!r}",
            details={"capability": capability, "offering": offering},
        )


class DaemonUnavailable(PymtHouseError):
    def __init__(self, *, daemon: str, reason: str) -> None:
        super().__init__(
            status_code=503,
            code="DAEMON_UNAVAILABLE",
            message=f"{daemon} unavailable: {reason}",
            details={"daemon": daemon, "reason": reason},
        )


class DuplicateRequest(PymtHouseError):
    def __init__(self) -> None:
        super().__init__(
            status_code=409,
            code="DUPLICATE_REQUEST",
            message="A request with this Idempotency-Key is already in flight",
        )


async def _handle(_request: Request, exc: PymtHouseError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "code": exc.code,
                "message": exc.message,
                "details": exc.details,
            }
        },
    )


def register_handlers(app: FastAPI) -> None:
    """Register the PymtHouseError exception handler on a FastAPI app."""
    app.add_exception_handler(PymtHouseError, _handle)  # type: ignore[arg-type]
