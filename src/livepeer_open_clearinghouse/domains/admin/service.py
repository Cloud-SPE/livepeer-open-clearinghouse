"""Business logic for admin."""

from __future__ import annotations

import hashlib
import secrets
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from livepeer_open_clearinghouse.domains.accounts import service as accounts_service
from livepeer_open_clearinghouse.domains.accounts.repo import OperatorApproval, User
from livepeer_open_clearinghouse.domains.admin.repo import Operator, OperatorAudit
from livepeer_open_clearinghouse.domains.billing import service as billing_service
from livepeer_open_clearinghouse.domains.billing.repo import CreditBalance
from livepeer_open_clearinghouse.providers.clock import Clock
from livepeer_open_clearinghouse.providers.email import EmailProvider, templates
from livepeer_open_clearinghouse.providers.telemetry import get_logger

logger = get_logger(__name__)

BOOTSTRAP_OPERATOR_EMAIL = "bootstrap@livepeer-open-clearinghouse.local"
BOOTSTRAP_OPERATOR_NAME = "Bootstrap Operator"


class AdminServiceError(Exception):
    code = "admin_error"


class UserAlreadyApproved(AdminServiceError):
    code = "user_already_approved"


class UserNotFound(AdminServiceError):
    code = "user_not_found"


class EmailAlreadyVerified(AdminServiceError):
    code = "email_already_verified"


def _hash_bootstrap_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def ensure_bootstrap_operator(session: AsyncSession, *, bootstrap_token: str) -> Operator:
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
        # The bootstrap operator owns the org until they create more.
        # Python-level default on the Operator model is "member" so we
        # have to override here explicitly.
        role="owner",
    )
    session.add(op)
    await session.flush()
    return op


async def authenticate_operator(session: AsyncSession, *, bearer_token: str) -> Operator | None:
    """Return the operator matching a bearer token, or None."""
    token_hash = _hash_bootstrap_token(bearer_token)
    return await session.scalar(
        select(Operator).where(Operator.token_hash == token_hash, Operator.revoked_at.is_(None))
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
            (OperatorApproval.user_id == User.id) & (OperatorApproval.revoked_at.is_(None)),
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


async def list_audit_entries(
    session: AsyncSession, *, limit: int = 100
) -> list[tuple[OperatorAudit, str, str | None]]:
    """Most-recent-first audit rows joined with operator + target-user emails.

    Returns ``(OperatorAudit, operator_email, target_user_email|None)``.
    """
    q = (
        select(OperatorAudit, Operator.email, User.email)
        .join(Operator, OperatorAudit.operator_id == Operator.id)
        .outerjoin(User, OperatorAudit.target_user_id == User.id)
        .order_by(OperatorAudit.created_at.desc())
        .limit(limit)
    )
    raw = await session.execute(q)
    return [(row[0], row[1], row[2]) for row in raw]


async def list_pending_users(session: AsyncSession) -> list[User]:
    """Users that signed up but have no active approval."""
    rows = await session.scalars(
        select(User)
        .outerjoin(
            OperatorApproval,
            (OperatorApproval.user_id == User.id) & (OperatorApproval.revoked_at.is_(None)),
        )
        .where(OperatorApproval.id.is_(None))
        .order_by(User.created_at.asc())
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
        except Exception as exc:
            logger.warning(
                "admin.approve_user.notification_failed",
                user_id=str(user_id),
                error=str(exc),
            )

    return approval


async def resend_verification(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    operator: Operator,
    clock: Clock,
    email_provider: EmailProvider,
    public_base_url: str,
) -> User:
    """Operator-triggered: mint + send a fresh email-verification token.

    Useful when the original email was lost / bounced / the user's
    self-hosted mail rejected it. Old unconsumed tokens for this user
    stay valid until their TTL — the user can use whichever link they
    actually receive.
    """
    user = await session.get(User, user_id)
    if user is None:
        raise UserNotFound
    if user.email_verified_at is not None:
        raise EmailAlreadyVerified

    await accounts_service.send_verification_email(
        session,
        user=user,
        clock=clock,
        email_provider=email_provider,
        public_base_url=public_base_url,
    )
    session.add(
        OperatorAudit(
            operator_id=operator.id,
            action="resend_verification",
            target_user_id=user_id,
        )
    )
    return user


# ---------------------------------------------------------------------------
# Operator management — owner-only mutations
# ---------------------------------------------------------------------------

OPERATOR_ROLES: tuple[str, ...] = ("owner", "member")
OPERATOR_TOKEN_PREFIX = "loc_op_"  # noqa: S105 — token prefix, not a credential


class InvalidOperatorRole(AdminServiceError):
    code = "invalid_operator_role"


class OperatorNotFound(AdminServiceError):
    code = "operator_not_found"


class OperatorEmailTaken(AdminServiceError):
    code = "operator_email_taken"


class CannotRevokeSelf(AdminServiceError):
    code = "cannot_revoke_self"


class CannotDemoteLastOwner(AdminServiceError):
    code = "cannot_demote_last_owner"


def _generate_operator_token() -> tuple[str, str]:
    """Return (raw_token, sha256_hash) for a new operator.

    The raw token is shown to the caller exactly once at creation /
    rotation; the hash is what we store. Same hashing primitive as the
    bootstrap token so the auth path doesn't need to branch on which
    flavour of token came in.
    """
    raw = OPERATOR_TOKEN_PREFIX + secrets.token_urlsafe(32)
    return raw, _hash_bootstrap_token(raw)


async def _count_active_owners(session: AsyncSession) -> int:
    n = await session.scalar(
        select(func.count())
        .select_from(Operator)
        .where(Operator.role == "owner", Operator.revoked_at.is_(None))
    )
    return int(n or 0)


async def list_operators(session: AsyncSession) -> list[Operator]:
    """Most-recent-first list of every operator (including revoked)."""
    rows = await session.scalars(select(Operator).order_by(Operator.created_at.desc()))
    return list(rows)


async def create_operator(
    session: AsyncSession,
    *,
    acting_operator: Operator,
    email: str,
    name: str,
    role: str = "member",
    clock: Clock,
) -> tuple[Operator, str]:
    """Create a new operator. Returns (row, raw_token)."""
    if role not in OPERATOR_ROLES:
        raise InvalidOperatorRole
    existing = await session.scalar(select(Operator).where(Operator.email == email))
    if existing is not None:
        raise OperatorEmailTaken
    raw, token_hash = _generate_operator_token()
    op = Operator(
        email=email,
        name=name,
        token_hash=token_hash,
        role=role,
    )
    session.add(op)
    await session.flush()
    session.add(
        OperatorAudit(
            operator_id=acting_operator.id,
            action="create_operator",
            target_user_id=None,
            params={"new_operator_id": str(op.id), "email": email, "role": role},
        )
    )
    return op, raw


async def update_operator(
    session: AsyncSession,
    *,
    acting_operator: Operator,
    operator_id: uuid.UUID,
    name: str | None = None,
    role: str | None = None,
    clock: Clock,
) -> Operator:
    target = await session.get(Operator, operator_id)
    if target is None:
        raise OperatorNotFound
    if role is not None and role not in OPERATOR_ROLES:
        raise InvalidOperatorRole

    # Don't allow demoting the last active owner — bricks the org.
    if (
        role is not None
        and role != "owner"
        and target.role == "owner"
        and target.revoked_at is None
        and await _count_active_owners(session) <= 1
    ):
        raise CannotDemoteLastOwner

    changes: dict[str, str] = {}
    if name is not None and name != target.name:
        target.name = name
        changes["name"] = name
    if role is not None and role != target.role:
        target.role = role
        changes["role"] = role
    if changes:
        session.add(
            OperatorAudit(
                operator_id=acting_operator.id,
                action="update_operator",
                target_user_id=None,
                params={"target_operator_id": str(operator_id), "changes": changes},
            )
        )
    await session.flush()
    return target


async def revoke_operator(
    session: AsyncSession,
    *,
    acting_operator: Operator,
    operator_id: uuid.UUID,
    clock: Clock,
) -> Operator:
    if operator_id == acting_operator.id:
        raise CannotRevokeSelf
    target = await session.get(Operator, operator_id)
    if target is None:
        raise OperatorNotFound
    if target.revoked_at is not None:
        return target
    # Can't revoke the last active owner.
    if target.role == "owner" and await _count_active_owners(session) <= 1:
        raise CannotDemoteLastOwner
    target.revoked_at = clock.now()
    session.add(
        OperatorAudit(
            operator_id=acting_operator.id,
            action="revoke_operator",
            target_user_id=None,
            params={"target_operator_id": str(operator_id)},
        )
    )
    await session.flush()
    return target


async def rotate_operator_token(
    session: AsyncSession,
    *,
    acting_operator: Operator,
    operator_id: uuid.UUID,
    clock: Clock,
) -> tuple[Operator, str]:
    """Mint a new bearer token for the target operator. Returns (row, raw_token).

    Caller-authz: the runtime layer enforces that the acting operator is
    either an owner OR the target themselves. Service-layer is identity-
    blind; just does the work.
    """
    target = await session.get(Operator, operator_id)
    if target is None:
        raise OperatorNotFound
    raw, token_hash = _generate_operator_token()
    target.token_hash = token_hash
    session.add(
        OperatorAudit(
            operator_id=acting_operator.id,
            action="rotate_operator_token",
            target_user_id=None,
            params={"target_operator_id": str(operator_id)},
        )
    )
    await session.flush()
    return target, raw
