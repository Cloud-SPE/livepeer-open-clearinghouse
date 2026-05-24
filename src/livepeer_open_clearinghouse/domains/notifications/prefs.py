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


async def notify_cap_reached(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    which_cap: str,
    remaining_wei: int,
    clock: Clock,
    independent_session_factory: IndependentSessionFactory | None = None,
) -> dict[str, bool]:
    """Fire the cap_reached trigger for a user. Best-effort.

    Looks up the user's email + the default email provider internally
    so the caller (the server-event emit helpers) doesn't have to
    thread them. Returns the per-channel fired map, or ``{}`` if the
    user record can't be loaded (silent — never raises).

    Cross-transaction subtlety: cap_reached typically fires from a
    refusal path that's about to raise — and the raise rolls back the
    request session, taking the portal_notification + email-send-log
    row with it. To make the customer-visible artifact survive the
    rollback, the in-portal write uses an *independent* session
    opened against the global engine. The email send is naturally
    independent (it's an outbound HTTP call). The ``session``
    parameter is kept only for the user-lookup convenience — that
    read is safe to share with the outer transaction.
    """
    try:
        user = await session.get(User, user_id)
    except Exception as exc:
        logger.warning("notify_cap_reached.user_lookup_failed", error=str(exc))
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
        logger.warning("notify_cap_reached.email_provider_unavailable", error=str(exc))
        provider = None
    subject = f"{BRAND_PREFIX} spend cap reached: {which_cap}"
    body = {
        "trigger": TRIGGER_CAP_REACHED,
        "which_cap": which_cap,
        "remaining_wei": remaining_wei,
    }

    # Resolve which channels this user has enabled.
    overrides = await list_overrides_for_user(session, user_id=user_id)
    fired: dict[str, bool] = {}
    enabled_email = _pref(overrides, TRIGGER_CAP_REACHED, CHANNEL_EMAIL)
    enabled_portal = _pref(overrides, TRIGGER_CAP_REACHED, CHANNEL_IN_PORTAL)

    if enabled_portal:
        factory = independent_session_factory or _default_independent_session_factory
        fired[CHANNEL_IN_PORTAL] = await _fire_in_portal_independent(
            user_id=user_id,
            trigger=TRIGGER_CAP_REACHED,
            body=body,
            clock=clock,
            factory=factory,
        )
    if enabled_email:
        fired[CHANNEL_EMAIL] = await _fire_email(
            provider, to=user.email, subject=subject, body=body
        )
    return fired


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
