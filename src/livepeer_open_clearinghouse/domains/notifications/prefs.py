"""Notification preferences + trigger fan-out.

Customer-facing notification system from exec-plan 002 §"Customer
notification preferences". Two responsibilities:

1. **Resolve preferences.** Combine the operator-set defaults with
   the customer's per-(trigger, channel) overrides in
   ``notification_config``. A missing row means "use the default."

2. **Fire a trigger.** ``notify(trigger, user_id, body)`` checks the
   resolved prefs for each channel, fires the corresponding action
   (write a ``portal_notification`` row, send an email), and never
   raises into the caller. Telemetry-style best-effort.

v1 scope (this PR — PR-5): three triggers, two channels.

  Triggers wired now: ``cap_reached``
  Triggers wired in PR-5b: ``period_rollover``, ``winddown_warning``,
    ``sdk_outdated``, ``session_failed_repeatedly``

  Channels wired now: ``email``, ``in_portal``
  Channels wired in PR-5b: ``webhook`` (opt-in, Standard-Webhooks signed)

The 5-trigger + 3-channel matrix and default values come from the
design doc. Future triggers/channels add by extending the constants
+ the action map.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from livepeer_open_clearinghouse.domains.accounts.repo import User
from livepeer_open_clearinghouse.domains.notifications.repo import (
    NotificationConfig,
    PortalNotification,
)
from livepeer_open_clearinghouse.providers.clock import Clock
from livepeer_open_clearinghouse.providers.email import EmailProvider, templates
from livepeer_open_clearinghouse.providers.telemetry import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Trigger + channel constants
# ---------------------------------------------------------------------------


TRIGGER_CAP_REACHED = "cap_reached"
TRIGGER_PERIOD_ROLLOVER = "period_rollover"
TRIGGER_WINDDOWN_WARNING = "winddown_warning"
TRIGGER_SDK_OUTDATED = "sdk_outdated"
TRIGGER_SESSION_FAILED_REPEATEDLY = "session_failed_repeatedly"

ALL_TRIGGERS: frozenset[str] = frozenset(
    {
        TRIGGER_CAP_REACHED,
        TRIGGER_PERIOD_ROLLOVER,
        TRIGGER_WINDDOWN_WARNING,
        TRIGGER_SDK_OUTDATED,
        TRIGGER_SESSION_FAILED_REPEATEDLY,
    }
)

CHANNEL_EMAIL = "email"
CHANNEL_IN_PORTAL = "in_portal"
CHANNEL_WEBHOOK = "webhook"  # reserved; wires in PR-5b

ALL_CHANNELS: frozenset[str] = frozenset(
    {CHANNEL_EMAIL, CHANNEL_IN_PORTAL, CHANNEL_WEBHOOK}
)


# Operator defaults per (trigger, channel). Customer can override via
# notification_config; absence means the default applies. Mirrors the
# table at the bottom of exec-plan 002 §"Defaults".
DEFAULTS: Mapping[tuple[str, str], bool] = {
    (TRIGGER_CAP_REACHED, CHANNEL_EMAIL): True,
    (TRIGGER_CAP_REACHED, CHANNEL_IN_PORTAL): True,
    (TRIGGER_CAP_REACHED, CHANNEL_WEBHOOK): False,

    (TRIGGER_PERIOD_ROLLOVER, CHANNEL_EMAIL): False,
    (TRIGGER_PERIOD_ROLLOVER, CHANNEL_IN_PORTAL): True,
    (TRIGGER_PERIOD_ROLLOVER, CHANNEL_WEBHOOK): False,

    (TRIGGER_WINDDOWN_WARNING, CHANNEL_EMAIL): True,
    (TRIGGER_WINDDOWN_WARNING, CHANNEL_IN_PORTAL): True,
    (TRIGGER_WINDDOWN_WARNING, CHANNEL_WEBHOOK): False,

    (TRIGGER_SDK_OUTDATED, CHANNEL_EMAIL): True,
    (TRIGGER_SDK_OUTDATED, CHANNEL_IN_PORTAL): False,
    (TRIGGER_SDK_OUTDATED, CHANNEL_WEBHOOK): False,

    (TRIGGER_SESSION_FAILED_REPEATEDLY, CHANNEL_EMAIL): True,
    (TRIGGER_SESSION_FAILED_REPEATEDLY, CHANNEL_IN_PORTAL): False,
    (TRIGGER_SESSION_FAILED_REPEATEDLY, CHANNEL_WEBHOOK): False,
}


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class NotificationServiceError(Exception):
    code = "notification_error"


class InvalidTrigger(NotificationServiceError):
    code = "invalid_trigger"


class InvalidChannel(NotificationServiceError):
    code = "invalid_channel"


class PortalNotificationNotFound(NotificationServiceError):
    code = "portal_notification_not_found"


# ---------------------------------------------------------------------------
# Preferences read/write
# ---------------------------------------------------------------------------


async def list_overrides_for_user(
    session: AsyncSession, *, user_id: uuid.UUID
) -> list[NotificationConfig]:
    rows = await session.scalars(
        select(NotificationConfig).where(NotificationConfig.user_id == user_id)
    )
    return list(rows)


async def resolved_prefs_for_user(
    session: AsyncSession, *, user_id: uuid.UUID
) -> dict[tuple[str, str], bool]:
    """Return the full (trigger, channel) → enabled map for a user.

    Starts from :data:`DEFAULTS`, overlays any rows in
    ``notification_config``. Always returns every cell in the
    cross-product so the portal UI can render the matrix without
    further default lookups.
    """
    out = dict(DEFAULTS)
    for row in await list_overrides_for_user(session, user_id=user_id):
        out[(row.trigger, row.channel)] = bool(row.enabled)
    return out


async def set_preference(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    trigger: str,
    channel: str,
    enabled: bool,
) -> NotificationConfig:
    """Insert or update one override row. Validates trigger/channel
    against the allow-list."""
    if trigger not in ALL_TRIGGERS:
        raise InvalidTrigger(f"unknown trigger {trigger!r}")
    if channel not in ALL_CHANNELS:
        raise InvalidChannel(f"unknown channel {channel!r}")
    existing = await session.get(NotificationConfig, (user_id, trigger, channel))
    if existing is not None:
        existing.enabled = enabled
        await session.flush()
        return existing
    row = NotificationConfig(
        user_id=user_id, trigger=trigger, channel=channel, enabled=enabled
    )
    session.add(row)
    await session.flush()
    return row


# ---------------------------------------------------------------------------
# Portal notification feed
# ---------------------------------------------------------------------------


async def list_active_portal_notifications(
    session: AsyncSession, *, user_id: uuid.UUID, limit: int = 50
) -> list[PortalNotification]:
    """List undismissed portal banners, newest first."""
    rows = await session.scalars(
        select(PortalNotification)
        .where(
            PortalNotification.user_id == user_id,
            PortalNotification.dismissed_at.is_(None),
        )
        .order_by(PortalNotification.fired_at.desc())
        .limit(limit)
    )
    return list(rows)


async def dismiss_portal_notification(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    notification_id: uuid.UUID,
    clock: Clock,
) -> PortalNotification:
    row = await session.get(PortalNotification, notification_id)
    if row is None or row.user_id != user_id:
        raise PortalNotificationNotFound
    if row.dismissed_at is None:
        row.dismissed_at = clock.now()
        await session.flush()
    return row


# ---------------------------------------------------------------------------
# Fire a trigger
# ---------------------------------------------------------------------------


async def notify(
    session: AsyncSession,
    *,
    trigger: str,
    user_id: uuid.UUID,
    user_email: str,
    subject: str,
    body: dict[str, object],
    clock: Clock,
    email_provider: EmailProvider | None,
) -> dict[str, bool]:
    """Fire one trigger for one user across all channels they have
    enabled.

    Returns a ``{channel: fired_bool}`` map. Each channel action is
    wrapped in try/except — a failure on one channel doesn't sink
    the others, and nothing propagates up to the caller. Best-effort,
    fire-and-forget, matching the design's "telemetry never breaks
    the data plane" posture.

    ``body`` is stored verbatim in ``portal_notification.body`` and
    passed to the email template. The subject is supplied by the
    caller (each trigger has its own message).
    """
    if trigger not in ALL_TRIGGERS:
        logger.warning("notify.invalid_trigger", trigger=trigger)
        return {}

    # Resolve which channels are enabled for this user + trigger.
    overrides = await list_overrides_for_user(session, user_id=user_id)
    enabled: dict[str, bool] = {}
    for channel in ALL_CHANNELS:
        # Per-user override beats default.
        override = next(
            (o for o in overrides if o.trigger == trigger and o.channel == channel),
            None,
        )
        enabled[channel] = (
            bool(override.enabled)
            if override is not None
            else DEFAULTS.get((trigger, channel), False)
        )

    fired: dict[str, bool] = {}
    if enabled[CHANNEL_IN_PORTAL]:
        fired[CHANNEL_IN_PORTAL] = await _fire_in_portal(
            session, user_id=user_id, trigger=trigger, body=body, clock=clock
        )
    if enabled[CHANNEL_EMAIL]:
        fired[CHANNEL_EMAIL] = await _fire_email(
            email_provider,
            to=user_email,
            subject=subject,
            body=body,
        )
    # CHANNEL_WEBHOOK reserved for PR-5b.
    return fired


async def _fire_in_portal(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    trigger: str,
    body: dict[str, object],
    clock: Clock,
) -> bool:
    try:
        session.add(
            PortalNotification(
                user_id=user_id,
                trigger=trigger,
                body=body,
                fired_at=clock.now(),
                dismissed_at=None,
            )
        )
        await session.flush()
        return True
    except Exception as exc:
        logger.warning(
            "notify.in_portal.failed",
            user_id=str(user_id),
            trigger=trigger,
            error=str(exc),
        )
        return False


# ---------------------------------------------------------------------------
# Convenience: per-trigger helpers used by the server-event emit path
# ---------------------------------------------------------------------------


IndependentSessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


def _default_independent_session_factory() -> AbstractAsyncContextManager[AsyncSession]:
    """Open a fresh session against the global engine. Imported lazily
    so tests can construct prefs without booting the engine."""
    from livepeer_open_clearinghouse.providers.db.engine import (  # noqa: PLC0415
        session_scope,
    )

    return session_scope()


async def _notify_trigger(
    session: AsyncSession,
    *,
    trigger: str,
    user_id: uuid.UUID,
    subject: str,
    body: dict[str, object],
    clock: Clock,
    independent_session_factory: IndependentSessionFactory | None = None,
) -> dict[str, bool]:
    """Shared fan-out for failure-path triggers — the trigger fires
    from inside a request transaction that's about to roll back.

    Looks up the user + email provider internally, resolves
    preferences, writes the portal banner through an independent
    session (so it survives the rollback), and sends the email
    out-of-transaction (HTTP call). Best-effort; never raises into
    the data plane.
    """
    try:
        user = await session.get(User, user_id)
    except Exception as exc:
        logger.warning(
            "notify._notify_trigger.user_lookup_failed",
            trigger=trigger,
            error=str(exc),
        )
        return {}
    if user is None:
        return {}
    provider: EmailProvider | None
    try:
        from livepeer_open_clearinghouse.dependencies import (  # noqa: PLC0415
            _default_email,
        )

        provider = _default_email()
    except Exception as exc:
        logger.warning(
            "notify._notify_trigger.email_provider_unavailable",
            trigger=trigger,
            error=str(exc),
        )
        provider = None

    overrides = await list_overrides_for_user(session, user_id=user_id)
    fired: dict[str, bool] = {}
    if _pref(overrides, trigger, CHANNEL_IN_PORTAL):
        factory = independent_session_factory or _default_independent_session_factory
        fired[CHANNEL_IN_PORTAL] = await _fire_in_portal_independent(
            user_id=user_id,
            trigger=trigger,
            body=body,
            clock=clock,
            factory=factory,
        )
    if _pref(overrides, trigger, CHANNEL_EMAIL):
        fired[CHANNEL_EMAIL] = await _fire_email(
            provider, to=user.email, subject=subject, body=body
        )
    if _pref(overrides, trigger, CHANNEL_WEBHOOK):
        fired[CHANNEL_WEBHOOK] = await _fire_webhook(
            session, user_id=user_id, trigger=trigger, body=body
        )
    return fired


async def notify_cap_reached(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    which_cap: str,
    remaining_wei: int,
    clock: Clock,
    independent_session_factory: IndependentSessionFactory | None = None,
) -> dict[str, bool]:
    """Fire the cap_reached trigger for a user. Best-effort."""
    return await _notify_trigger(
        session,
        trigger=TRIGGER_CAP_REACHED,
        user_id=user_id,
        subject=f"{BRAND_PREFIX} spend cap reached: {which_cap}",
        body={
            "trigger": TRIGGER_CAP_REACHED,
            "which_cap": which_cap,
            "remaining_wei": remaining_wei,
        },
        clock=clock,
        independent_session_factory=independent_session_factory,
    )


async def notify_winddown_warning(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    session_id: uuid.UUID,
    reason: str,
    clock: Clock,
    independent_session_factory: IndependentSessionFactory | None = None,
) -> dict[str, bool]:
    """Fire when a refill response carries
    ``cap_status.will_refuse_next_refill=true`` — the next refill on
    this session will be rejected, so the customer should plan to
    wind down."""
    return await _notify_trigger(
        session,
        trigger=TRIGGER_WINDDOWN_WARNING,
        user_id=user_id,
        subject=f"{BRAND_PREFIX} session winding down: {reason}",
        body={
            "trigger": TRIGGER_WINDDOWN_WARNING,
            "session_id": str(session_id),
            "reason": reason,
        },
        clock=clock,
        independent_session_factory=independent_session_factory,
    )


async def notify_sdk_outdated(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    lang: str,
    semver: str,
    observed_status: str,
    clock: Clock,
    independent_session_factory: IndependentSessionFactory | None = None,
) -> dict[str, bool]:
    """Fire when LOC observes an SDK identity that isn't approved.

    The server-side `_maybe_notify_sdk_outdated` wrapper at the
    emit-helper layer applies a per-user dedupe TTL so the customer
    doesn't get one notification per request — see
    `telemetry.server_events`. This helper itself just runs the
    fan-out.
    """
    return await _notify_trigger(
        session,
        trigger=TRIGGER_SDK_OUTDATED,
        user_id=user_id,
        subject=f"{BRAND_PREFIX} SDK out of date: {lang} {semver}",
        body={
            "trigger": TRIGGER_SDK_OUTDATED,
            "lang": lang,
            "semver": semver,
            "observed_status": observed_status,
        },
        clock=clock,
        independent_session_factory=independent_session_factory,
    )


async def notify_period_rollover(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    period_start: str,
    period_end: str,
    previous_period_spend_wei: int,
    clock: Clock,
    independent_session_factory: IndependentSessionFactory | None = None,
) -> dict[str, bool]:
    """Fire when a customer's spend-period window rolls over —
    surfaces 'you have a fresh quota for the new window' in the
    portal."""
    return await _notify_trigger(
        session,
        trigger=TRIGGER_PERIOD_ROLLOVER,
        user_id=user_id,
        subject=f"{BRAND_PREFIX} spend period rolled over",
        body={
            "trigger": TRIGGER_PERIOD_ROLLOVER,
            "period_start": period_start,
            "period_end": period_end,
            "previous_period_spend_wei": previous_period_spend_wei,
        },
        clock=clock,
        independent_session_factory=independent_session_factory,
    )


async def notify_session_failed_repeatedly(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    api_key_id: uuid.UUID,
    failure_count: int,
    window_hours: int,
    clock: Clock,
    independent_session_factory: IndependentSessionFactory | None = None,
) -> dict[str, bool]:
    """Fire when ≥3 ``session.error`` events for one API key land
    inside the last hour. Surfaces 'something is repeatedly going
    wrong' faster than a customer would notice from the dashboard.
    """
    return await _notify_trigger(
        session,
        trigger=TRIGGER_SESSION_FAILED_REPEATEDLY,
        user_id=user_id,
        subject=f"{BRAND_PREFIX} session failures detected",
        body={
            "trigger": TRIGGER_SESSION_FAILED_REPEATEDLY,
            "api_key_id": str(api_key_id),
            "failure_count": failure_count,
            "window_hours": window_hours,
        },
        clock=clock,
        independent_session_factory=independent_session_factory,
    )


def _pref(
    overrides: list[NotificationConfig], trigger: str, channel: str
) -> bool:
    """Lookup the (trigger, channel) preference: per-user override
    beats default. Helper for notify_cap_reached's two-channel fan-out;
    avoids re-computing the full matrix when only two cells matter."""
    override = next(
        (o for o in overrides if o.trigger == trigger and o.channel == channel),
        None,
    )
    if override is not None:
        return bool(override.enabled)
    return DEFAULTS.get((trigger, channel), False)


async def _fire_in_portal_independent(
    *,
    user_id: uuid.UUID,
    trigger: str,
    body: dict[str, object],
    clock: Clock,
    factory: IndependentSessionFactory,
) -> bool:
    """Write a portal_notification row in its own session.

    Used by failure-path triggers so the customer-visible banner
    survives the outer request's rollback.
    """
    try:
        async with factory() as db:
            db.add(
                PortalNotification(
                    user_id=user_id,
                    trigger=trigger,
                    body=body,
                    fired_at=clock.now(),
                    dismissed_at=None,
                )
            )
        return True
    except Exception as exc:
        logger.warning(
            "notify.in_portal_independent.failed",
            user_id=str(user_id),
            trigger=trigger,
            error=str(exc),
        )
        return False




BRAND_PREFIX = "Livepeer Open Clearinghouse"


async def _fire_email(
    provider: EmailProvider | None,
    *,
    to: str,
    subject: str,
    body: dict[str, object],
) -> bool:
    if provider is None:
        return False
    try:
        message = templates.notification_email(
            to=to,
            subject=subject,
            body=body,
        )
        await provider.send(message)
        return True
    except Exception as exc:
        logger.warning(
            "notify.email.failed",
            to=to,
            subject=subject,
            error=str(exc),
        )
        return False


async def _fire_webhook(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    trigger: str,
    body: dict[str, object],
) -> bool:
    """Look up the user's webhook config + POST the signed payload.

    Returns True on a 2xx response within the configured retry
    budget; False when the user has no webhook configured, the
    server seed isn't set, or the send exhausts retries. Best-effort
    like every other channel — never raises.
    """
    try:
        from livepeer_open_clearinghouse.domains.notifications.repo import (  # noqa: PLC0415
            NotificationWebhookConfig,
        )
        from livepeer_open_clearinghouse.domains.notifications.webhook import (  # noqa: PLC0415
            derive_secret,
            send_webhook,
        )
        from livepeer_open_clearinghouse.settings import get_settings  # noqa: PLC0415

        row = await session.get(NotificationWebhookConfig, user_id)
        if row is None:
            return False
        settings = get_settings()
        if settings.webhook_signing_seed is None:
            return False
        secret = derive_secret(
            settings.webhook_signing_seed.get_secret_value(), user_id
        )
        return await send_webhook(
            http=None,
            url=row.url,
            secret=secret,
            payload={"trigger": trigger, "body": body},
            max_retries=settings.webhook_send_max_retries,
            timeout_seconds=settings.webhook_send_timeout_seconds,
        )
    except Exception as exc:
        logger.warning(
            "notify.webhook.failed",
            user_id=str(user_id),
            trigger=trigger,
            error=str(exc),
        )
        return False
