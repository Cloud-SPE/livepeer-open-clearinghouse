"""ORM models and queries for the accounts domain.

Tables:
    user                        — primary identity
    user_email_verification     — single-use email verification tokens
    user_oauth_identity         — linked Google/GitHub identities
    user_session                — portal session tokens
    operator_approval           — per-user operator approval ledger
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Index, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from livepeer_open_clearinghouse.providers.db import (
    Base,
    TableNameFromClassMixin,
    TimestampMixin,
    UuidPkMixin,
)


class User(Base, UuidPkMixin, TimestampMixin, TableNameFromClassMixin):
    """A Livepeer Open Clearinghouse user (app developer)."""

    email: Mapped[str] = mapped_column(unique=True, nullable=False)
    email_verified_at: Mapped[datetime | None] = mapped_column(nullable=True)
    password_hash: Mapped[str | None] = mapped_column(nullable=True)

    sessions: Mapped[list["UserSession"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class UserEmailVerification(Base, UuidPkMixin, TimestampMixin, TableNameFromClassMixin):
    """A single-use email verification token (hash stored, not the raw token)."""

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(nullable=False)
    expires_at: Mapped[datetime] = mapped_column(nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(nullable=True)


class UserPasswordReset(Base, UuidPkMixin, TimestampMixin, TableNameFromClassMixin):
    """A single-use password-reset token (hash stored, not the raw token)."""

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(nullable=False)
    expires_at: Mapped[datetime] = mapped_column(nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(nullable=True)


class UserOAuthIdentity(Base, UuidPkMixin, TimestampMixin, TableNameFromClassMixin):
    """A linked third-party identity (Google or GitHub)."""

    __table_args__ = (
        UniqueConstraint(
            "provider", "provider_user_id", name="uq_user_oauth_identity_provider_uid"
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(nullable=False)  # 'google' | 'github'
    provider_user_id: Mapped[str] = mapped_column(nullable=False)
    email_at_link: Mapped[str] = mapped_column(nullable=False)


class UserSession(Base, UuidPkMixin, TimestampMixin, TableNameFromClassMixin):
    """A portal web session (token hash stored, not the raw token)."""

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(nullable=True)

    user: Mapped[User] = relationship(back_populates="sessions")


class OperatorApproval(Base, UuidPkMixin, TimestampMixin, TableNameFromClassMixin):
    """A record of an operator approving (or revoking) a user.

    Multiple historical rows per user are allowed; the unique partial index
    enforces "at most one active approval per user."
    """

    __table_args__ = (
        Index(
            "uq_operator_approval_active_per_user",
            "user_id",
            unique=True,
            postgresql_where="revoked_at IS NULL",
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE"), nullable=False
    )
    operator_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("operator.id", ondelete="RESTRICT"), nullable=False
    )
    approved_at: Mapped[datetime] = mapped_column(nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(nullable=True)
    revocation_reason: Mapped[str | None] = mapped_column(nullable=True)
