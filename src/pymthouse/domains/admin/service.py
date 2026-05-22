"""Business logic for admin."""

from __future__ import annotations

import hashlib
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from pymthouse.domains.accounts.repo import OperatorApproval, User
from pymthouse.domains.admin.repo import Operator, OperatorAudit
from pymthouse.domains.billing import service as billing_service
from pymthouse.domains.billing.repo import CreditBalance
from pymthouse.providers.clock import Clock
from pymthouse.providers.email import EmailProvider, templates
from pymthouse.providers.telemetry import get_logger

logger = get_logger(__name__)

BOOTSTRAP_OPERATOR_EMAIL = "bootstrap@pymthouse.local"
BOOTSTRAP_OPERATOR_NAME = "Bootstrap Operator"


class AdminServiceError(Exception):
    code = "admin_error"


class UserAlreadyApproved(AdminServiceError):
    code = "user_already_approved"


class UserNotFound(AdminServiceError):
    code = "user_not_found"


def _hash_bootstrap_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def ensure_bootstrap_operator(
    session: AsyncSession, *, bootstrap_token: str
) -> Operator:
    """Ensure the bootstrap operator row exists for the supplied token.

    Called at app startup. If a matching operator exists, returned unchanged;
    otherwise created. The token is stored hashed; the env var is the only
    place the raw token lives.
    """
    token_hash = _hash_bootstrap_token(bootstrap_token)
    existing = await session.scalar(
        select(Operator).where(Operator.email == BOOTSTRAP_OPERATOR_EMAIL)
    )
    if existing is not None:
        # Rotate the token in place if the env value changed.
        if existing.token_hash != token_hash:
            existing.token_hash = token_hash
        return existing
    op = Operator(
        email=BOOTSTRAP_OPERATOR_EMAIL,
        name=BOOTSTRAP_OPERATOR_NAME,
        token_hash=token_hash,
    )
    session.add(op)
    await session.flush()
    return op


async def authenticate_operator(
    session: AsyncSession, *, bearer_token: str
) -> Operator | None:
    """Return the operator matching a bearer token, or None."""
    token_hash = _hash_bootstrap_token(bearer_token)
    return await session.scalar(
        select(Operator).where(
            Operator.token_hash == token_hash, Operator.revoked_at.is_(None)
        )
    )


async def list_all_users(
    session: AsyncSession, *, limit: int = 100, offset: int = 0
) -> tuple[list[tuple[User, bool, int]], int]:
    """List every user with their approval status and current balance.

    Returns ``(rows, total_count)``. Each row is
    ``(User, is_approved, balance_wei)``.
    """
    total = await session.scalar(select(func.count()).select_from(User))
    rows_q = (
        select(
            User,
            OperatorApproval.id.is_not(None).label("approved"),
            func.coalesce(CreditBalance.amount_wei, 0).label("balance_wei"),
        )
        .outerjoin(
            OperatorApproval,
            (OperatorApproval.user_id == User.id)
            & (OperatorApproval.revoked_at.is_(None)),
        )
        .outerjoin(CreditBalance, CreditBalance.user_id == User.id)
        .order_by(User.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    raw = await session.execute(rows_q)
    rows: list[tuple[User, bool, int]] = [
        (r.User, bool(r.approved), int(r.balance_wei)) for r in raw
    ]
    return rows, int(total or 0)


async def list_pending_users(session: AsyncSession) -> list[User]:
    """Users that signed up but have no active approval."""
    rows = await session.scalars(
        select(User).outerjoin(
            OperatorApproval,
            (OperatorApproval.user_id == User.id)
            & (OperatorApproval.revoked_at.is_(None)),
        ).where(OperatorApproval.id.is_(None)).order_by(User.created_at.asc())
    )
    return list(rows)


async def approve_user(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    operator: Operator,
    clock: Clock,
    initial_credit_wei: int = 0,
    email_provider: EmailProvider | None = None,
    public_base_url: str | None = None,
) -> OperatorApproval:
    """Create an active operator_approval for `user_id` and grant initial credit.

    Same transaction as the caller's session: if the credit grant fails,
    the approval is rolled back. The notification email is best-effort —
    a send failure is logged but does not roll back the approval.
    """
    user = await session.get(User, user_id)
    if user is None:
        raise UserNotFound

    existing = await session.scalar(
        select(OperatorApproval).where(
            OperatorApproval.user_id == user_id,
            OperatorApproval.revoked_at.is_(None),
        )
    )
    if existing is not None:
        raise UserAlreadyApproved

    now = clock.now()
    approval = OperatorApproval(
        user_id=user_id,
        operator_id=operator.id,
        approved_at=now,
    )
    session.add(approval)
    session.add(
        OperatorAudit(
            operator_id=operator.id,
            action="approve_user",
            target_user_id=user_id,
        )
    )
    await session.flush()

    if initial_credit_wei > 0:
        await billing_service.grant_initial_credit(
            session,
            user_id=user_id,
            amount_wei=initial_credit_wei,
            operator_id=operator.id,
        )

    if email_provider is not None and public_base_url is not None:
        try:
            await email_provider.send(
                templates.approval_notification_email(
                    to=user.email, public_base_url=public_base_url
                )
            )
        except Exception as exc:  # noqa: BLE001 — best-effort notification
            logger.warning(
                "admin.approve_user.notification_failed",
                user_id=str(user_id),
                error=str(exc),
            )

    return approval
