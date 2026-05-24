"""Typed helpers for emitting ``server.*`` events from LOC's runtime.

Two design constraints:

1. **Telemetry failures must never break the data plane.** Every emit
   is wrapped in a try/except that logs at WARN but does NOT raise.
   If the telemetry table is unreachable, the mint / refill / close
   call that triggered the event still succeeds.

2. **Typed signatures over free-form payloads.** Each event type has
   its own helper with explicit args, so callers can't typo the
   field names and so a schema change touches the helper signature
   (and every caller) in one diff.

The seven v1 ``server.*`` events from exec-plan 002 §"LOC
server-side events (v1)":

    server.mint_served                — successful mint (job or session)
    server.mint_refused               — mint rejected by a cap or balance
    server.refill_served              — successful refill mint
    server.refill_denied              — refill rejected by a cap
    server.session_janitor_finalized  — janitor closed a session SDK never closed
    server.sdk_sha_mismatch           — SDK identity not approved
    server.discrepancy_detected       — SDK report vs daemon ledger diverge
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from livepeer_open_clearinghouse.domains.telemetry import service as telemetry_service
from livepeer_open_clearinghouse.domains.telemetry.config import CURRENT_SCHEMA_VERSION
from livepeer_open_clearinghouse.providers.clock import Clock
from livepeer_open_clearinghouse.providers.telemetry import get_logger

logger = get_logger(__name__)


async def _safe_emit(
    db: AsyncSession,
    *,
    event_type: str,
    payload: dict[str, object],
    api_key_id: uuid.UUID | None,
    user_id: uuid.UUID | None,
    correlation_id: uuid.UUID | None,
    clock: Clock,
) -> None:
    """Persist a server event; swallow + log any failure."""
    try:
        await telemetry_service.record_server_event(
            db,
            event_type=event_type,
            event_schema_version=CURRENT_SCHEMA_VERSION,
            payload=payload,
            api_key_id=api_key_id,
            user_id=user_id,
            correlation_id=correlation_id,
            clock=clock,
        )
    except Exception as exc:
        logger.warning(
            "telemetry.server_event.emit_failed",
            event_type=event_type,
            error=str(exc),
            error_class=exc.__class__.__name__,
        )


async def emit_mint_served(
    db: AsyncSession,
    *,
    api_key_id: uuid.UUID,
    user_id: uuid.UUID,
    capability: str,
    offering: str,
    mode: str,
    estimated_units: int,
    funded_value_wei: int,
    mint_latency_ms: int,
    correlation_id: uuid.UUID | None,
    clock: Clock,
) -> None:
    await _safe_emit(
        db,
        event_type="server.mint_served",
        payload={
            "capability": capability,
            "offering": offering,
            "mode": mode,
            "estimated_units": estimated_units,
            "funded_value_wei": funded_value_wei,
            "mint_latency_ms": mint_latency_ms,
        },
        api_key_id=api_key_id,
        user_id=user_id,
        correlation_id=correlation_id,
        clock=clock,
    )


async def emit_mint_refused(
    db: AsyncSession,
    *,
    api_key_id: uuid.UUID,
    user_id: uuid.UUID,
    capability: str | None,
    offering: str | None,
    which_cap: str,
    remaining_wei: int,
    clock: Clock,
) -> None:
    await _safe_emit(
        db,
        event_type="server.mint_refused",
        payload={
            "capability": capability,
            "offering": offering,
            "which_cap": which_cap,
            "remaining_wei": remaining_wei,
        },
        api_key_id=api_key_id,
        user_id=user_id,
        correlation_id=None,
        clock=clock,
    )


async def emit_refill_served(
    db: AsyncSession,
    *,
    api_key_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: uuid.UUID,
    refill_seq: int,
    funded_value_wei: int,
    cap_status: dict[str, object],
    clock: Clock,
) -> None:
    await _safe_emit(
        db,
        event_type="server.refill_served",
        payload={
            "session_id": str(session_id),
            "refill_seq": refill_seq,
            "funded_value_wei": funded_value_wei,
            "cap_status": cap_status,
        },
        api_key_id=api_key_id,
        user_id=user_id,
        correlation_id=session_id,
        clock=clock,
    )


async def emit_refill_denied(
    db: AsyncSession,
    *,
    api_key_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: uuid.UUID,
    refill_seq: int,
    which_cap: str,
    remaining_wei: int,
    clock: Clock,
) -> None:
    await _safe_emit(
        db,
        event_type="server.refill_denied",
        payload={
            "session_id": str(session_id),
            "refill_seq": refill_seq,
            "which_cap": which_cap,
            "remaining_wei": remaining_wei,
        },
        api_key_id=api_key_id,
        user_id=user_id,
        correlation_id=session_id,
        clock=clock,
    )


async def emit_session_janitor_finalized(
    db: AsyncSession,
    *,
    api_key_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: uuid.UUID,
    actual_units: int,
    billed_value_wei: int,
    refund_wei: int,
    outcome: str,
    silence_duration_seconds: int,
    clock: Clock,
) -> None:
    await _safe_emit(
        db,
        event_type="server.session_janitor_finalized",
        payload={
            "session_id": str(session_id),
            "actual_units": actual_units,
            "billed_value_wei": billed_value_wei,
            "refund_wei": refund_wei,
            "outcome": outcome,
            "silence_duration_seconds": silence_duration_seconds,
        },
        api_key_id=api_key_id,
        user_id=user_id,
        correlation_id=session_id,
        clock=clock,
    )


async def emit_sdk_sha_mismatch(
    db: AsyncSession,
    *,
    api_key_id: uuid.UUID,
    user_id: uuid.UUID,
    lang: str,
    semver: str,
    reported_sha: str,
    observed_status: str,
    clock: Clock,
) -> None:
    """Fired when LOC observes an SDK identity whose bucket is not
    ``approved`` (i.e. deprecated, blocked, or unknown). The
    "expected_sha" field in the spec is forward-looking — for now we
    emit ``observed_status`` instead so admin can filter on it. The
    field name flips when manifest pinning ships.
    """
    await _safe_emit(
        db,
        event_type="server.sdk_sha_mismatch",
        payload={
            "lang": lang,
            "semver": semver,
            "reported_sha": reported_sha,
            "observed_status": observed_status,
        },
        api_key_id=api_key_id,
        user_id=user_id,
        correlation_id=None,
        clock=clock,
    )


async def emit_sha_mismatch_if_unapproved(
    db: AsyncSession,
    *,
    api_key_id: uuid.UUID,
    user_id: uuid.UUID,
    sdk_identity: str | None,
    clock: Clock,
) -> None:
    """Fire :func:`emit_sdk_sha_mismatch` when the SDK identity is not
    bucketed as ``approved``. Covers unknown / deprecated / blocked.
    Imports admin_service lazily to avoid the telemetry → admin
    static-import direction (admin already imports from sessions which
    imports telemetry; this keeps the cycle inverted at runtime
    only).
    """
    from livepeer_open_clearinghouse.domains.admin import (  # noqa: PLC0415
        service as admin_service,
    )

    triple = admin_service.parse_sdk_identity(sdk_identity)
    if triple is None:
        await emit_sdk_sha_mismatch(
            db,
            api_key_id=api_key_id,
            user_id=user_id,
            lang="",
            semver="",
            reported_sha="",
            observed_status="unknown",
            clock=clock,
        )
        return
    status = await admin_service.evaluate_sdk_identity(db, sdk_identity=sdk_identity)
    if status == "approved":
        return
    lang, semver, sha = triple
    await emit_sdk_sha_mismatch(
        db,
        api_key_id=api_key_id,
        user_id=user_id,
        lang=lang,
        semver=semver,
        reported_sha=sha,
        observed_status=status,
        clock=clock,
    )


async def emit_discrepancy_detected(
    db: AsyncSession,
    *,
    api_key_id: uuid.UUID,
    user_id: uuid.UUID,
    job_or_session_id: uuid.UUID,
    sdk_reported_units: int,
    daemon_units: int,
    clock: Clock,
) -> None:
    """Settle-verification path: SDK reported a different actual_units
    than the daemon's ledger. Wired in when the daemon's
    GetSessionDebits is called inline on close (today only the janitor
    polls it; close_session trusts the SDK report). Helper lives here
    so the callsite is one-line when that wires up."""
    await _safe_emit(
        db,
        event_type="server.discrepancy_detected",
        payload={
            "job_or_session_id": str(job_or_session_id),
            "sdk_reported_units": sdk_reported_units,
            "daemon_units": daemon_units,
            "difference": sdk_reported_units - daemon_units,
        },
        api_key_id=api_key_id,
        user_id=user_id,
        correlation_id=job_or_session_id,
        clock=clock,
    )
