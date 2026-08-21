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

The active ``server.*`` events are:

    server.mint_served                — successful mint (job or session)
    server.mint_refused               — mint rejected by a cap or balance
    server.refill_served              — successful refill mint
    server.refill_denied              — refill rejected by a cap
    server.sdk_sha_mismatch           — SDK identity not approved
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

from sqlalchemy.ext.asyncio import AsyncSession

from livepeer_open_clearinghouse.domains.telemetry import service as telemetry_service
from livepeer_open_clearinghouse.domains.telemetry.config import CURRENT_SCHEMA_VERSION
from livepeer_open_clearinghouse.domains.telemetry.enrichment import (
    Enrichment,
    resolve_ingest_node_id,
)
from livepeer_open_clearinghouse.providers.clock import Clock
from livepeer_open_clearinghouse.providers.telemetry import get_logger
from livepeer_open_clearinghouse.settings import get_settings

logger = get_logger(__name__)


IndependentSessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


def _default_independent_session_factory(
    bound_session: AsyncSession | None = None,
) -> AbstractAsyncContextManager[AsyncSession]:
    """Open a fresh session for an independent write.

    Two modes:

      - ``bound_session=None`` (production default): open a session
        against the process-global engine via ``session_scope()``.
      - ``bound_session=...``: derive a sessionmaker from the passed-in
        session's engine and open a fresh session against it. This is
        the path that matters for tests, where the global engine isn't
        configured but a per-test engine is bound to ``bound_session``.

    Either way the new session is independent of ``bound_session``'s
    transaction — its commit is durable even if the outer rolls back.
    """
    if bound_session is not None:
        from contextlib import asynccontextmanager  # noqa: PLC0415

        from sqlalchemy.ext.asyncio import async_sessionmaker  # noqa: PLC0415

        engine = bound_session.bind
        maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

        from collections.abc import AsyncIterator  # noqa: PLC0415

        @asynccontextmanager
        async def _bound() -> AsyncIterator[AsyncSession]:
            async with maker() as session:
                try:
                    yield session
                    await session.commit()
                except Exception:
                    await session.rollback()
                    raise

        return _bound()

    from livepeer_open_clearinghouse.providers.db.engine import (  # noqa: PLC0415
        session_scope,
    )

    return session_scope()


def _server_side_enrichment() -> Enrichment:
    """Enrichment bundle for server-emitted events.

    No source IP (LOC is the source), no broker_url payload (not yet
    used). ``ingest_node_id`` is the only field with meaningful data;
    everything else is reserved for future expansion.

    Reads settings lazily so the test fixtures override correctly.
    """
    try:
        configured = get_settings().ingest_node_id
    except Exception:
        configured = None
    return Enrichment(ingest_node_id=resolve_ingest_node_id(configured))


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
            enrichment=_server_side_enrichment(),
        )
    except Exception as exc:
        logger.warning(
            "telemetry.server_event.emit_failed",
            event_type=event_type,
            error=str(exc),
            error_class=exc.__class__.__name__,
        )


async def _safe_emit_independent(
    *,
    event_type: str,
    payload: dict[str, object],
    api_key_id: uuid.UUID | None,
    user_id: uuid.UUID | None,
    correlation_id: uuid.UUID | None,
    clock: Clock,
    factory: IndependentSessionFactory | None = None,
    bound_session: AsyncSession | None = None,
) -> None:
    """Persist a server event through a fresh session.

    Used by refusal-path helpers (``emit_mint_refused``,
    ``emit_refill_denied``) where the caller is about to raise — the
    outer request transaction is therefore guaranteed to roll back,
    which would eat a row written on the passed-in session. The
    customer-facing portal banner already uses this pattern via
    ``_fire_in_portal_independent`` in the notifications module; this
    mirrors it for the server-event row itself so the operator-side
    audit trail (admin telemetry panel + DSAR-purgeable history) is
    not silently lost on refusals.

    Factory resolution: explicit ``factory`` arg wins; otherwise we
    open against the engine bound to ``bound_session`` when one is
    supplied; otherwise we open against the process-global engine.
    Tests typically pass ``bound_session=<their test session>`` so the
    independent write lands in the same per-test DB as the rest of
    their assertions.
    """
    open_session: IndependentSessionFactory
    if factory is None:
        open_session = lambda: _default_independent_session_factory(  # noqa: E731
            bound_session=bound_session
        )
    else:
        open_session = factory
    try:
        async with open_session() as db:
            await telemetry_service.record_server_event(
                db,
                event_type=event_type,
                event_schema_version=CURRENT_SCHEMA_VERSION,
                payload=payload,
                api_key_id=api_key_id,
                user_id=user_id,
                correlation_id=correlation_id,
                clock=clock,
                enrichment=_server_side_enrichment(),
            )
    except Exception as exc:
        logger.warning(
            "telemetry.server_event.emit_independent_failed",
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
    protocol: str,
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
            "protocol": protocol,
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
    independent_session_factory: IndependentSessionFactory | None = None,
) -> None:
    """Record a ``server.mint_refused`` event + fire cap_reached.

    The mint endpoint raises an HTTPException after this helper
    returns, so the request transaction will roll back. We write the
    telemetry row through an independent session (matching the
    portal_notification pattern in ``prefs._fire_in_portal_independent``)
    so the operator-side admin counts + DSAR history survive the
    rollback. Callers don't need to know about this — the helper is
    drop-in compatible with the old signature.
    """
    await _safe_emit_independent(
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
        factory=independent_session_factory,
        bound_session=db,
    )
    await _maybe_notify_cap_reached(
        db, user_id=user_id, which_cap=which_cap, remaining_wei=remaining_wei, clock=clock
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
    # When the cap-status block flags an imminent refusal, fire the
    # winddown_warning notification so the customer can plan.
    if cap_status.get("will_refuse_next_refill"):
        reason_obj = cap_status.get("winddown_reason")
        reason = str(reason_obj) if reason_obj else "cap_imminent"
        await _maybe_notify_winddown(
            db,
            user_id=user_id,
            session_id=session_id,
            reason=reason,
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
    independent_session_factory: IndependentSessionFactory | None = None,
) -> None:
    """Record a ``server.refill_denied`` event + fire cap_reached.

    Same independent-session rationale as :func:`emit_mint_refused` —
    the refill endpoint raises after this helper, so the row needs to
    survive the outer rollback.
    """
    await _safe_emit_independent(
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
        factory=independent_session_factory,
        bound_session=db,
    )
    await _maybe_notify_cap_reached(
        db, user_id=user_id, which_cap=which_cap, remaining_wei=remaining_wei, clock=clock
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
    await _maybe_notify_sdk_outdated(
        db,
        user_id=user_id,
        lang=lang,
        semver=semver,
        observed_status=observed_status,
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


async def _maybe_notify_cap_reached(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    which_cap: str,
    remaining_wei: int,
    clock: Clock,
) -> None:
    """Fire the notifications.cap_reached trigger after a refill/mint
    refusal. Best-effort: every failure path inside the notification
    pipeline is logged + swallowed, so a telemetry-side problem can
    never break the mint/refill data plane.

    Lazy-imports the prefs module to keep the static dep direction
    telemetry → notifications without forcing every importer of
    server_events to also import notifications.
    """
    try:
        from livepeer_open_clearinghouse.domains.notifications import (  # noqa: PLC0415
            prefs as notification_prefs,
        )

        await notification_prefs.notify_cap_reached(
            db,
            user_id=user_id,
            which_cap=which_cap,
            remaining_wei=remaining_wei,
            clock=clock,
        )
    except Exception as exc:
        logger.warning(
            "telemetry.cap_reached_notify_failed",
            user_id=str(user_id),
            error=str(exc),
        )


async def _maybe_notify_winddown(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    session_id: uuid.UUID,
    reason: str,
    clock: Clock,
) -> None:
    """Fire winddown_warning after a refill response sets
    will_refuse_next_refill=true. Best-effort."""
    try:
        from livepeer_open_clearinghouse.domains.notifications import (  # noqa: PLC0415
            prefs as notification_prefs,
        )

        await notification_prefs.notify_winddown_warning(
            db,
            user_id=user_id,
            session_id=session_id,
            reason=reason,
            clock=clock,
        )
    except Exception as exc:
        logger.warning(
            "telemetry.winddown_notify_failed",
            user_id=str(user_id),
            error=str(exc),
        )


# In-process TTL set used to dedupe sdk_outdated notifications. One
# notification per (user_id, lang, semver) tuple per
# SDK_OUTDATED_DEDUPE_HOURS — otherwise the customer drowns in mail
# (the trigger would fire on every mint they make with an old SDK).
SDK_OUTDATED_DEDUPE_HOURS = 24

_sdk_outdated_seen: dict[tuple[uuid.UUID, str, str], float] = {}


async def _maybe_notify_sdk_outdated(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    lang: str,
    semver: str,
    observed_status: str,
    clock: Clock,
) -> None:
    """Fire sdk_outdated when LOC observes an unapproved SDK identity,
    dedupe-throttled to once per ``(user_id, lang, semver)`` per
    SDK_OUTDATED_DEDUPE_HOURS so a customer running an old build
    doesn't get spammed."""
    if not lang:
        # The unknown-identity case (parse failed) carries an empty
        # lang/semver; skip the notification — the admin still sees
        # the server.* event.
        return
    import time  # noqa: PLC0415

    key = (user_id, lang, semver)
    now_ts = time.time()
    last = _sdk_outdated_seen.get(key)
    if last is not None and (now_ts - last) < SDK_OUTDATED_DEDUPE_HOURS * 3600:
        return
    _sdk_outdated_seen[key] = now_ts

    try:
        from livepeer_open_clearinghouse.domains.notifications import (  # noqa: PLC0415
            prefs as notification_prefs,
        )

        await notification_prefs.notify_sdk_outdated(
            db,
            user_id=user_id,
            lang=lang,
            semver=semver,
            observed_status=observed_status,
            clock=clock,
        )
    except Exception as exc:
        logger.warning(
            "telemetry.sdk_outdated_notify_failed",
            user_id=str(user_id),
            error=str(exc),
        )
