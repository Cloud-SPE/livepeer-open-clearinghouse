"""ORM models and queries for the admin domain.

Tables:
    operator        — humans who can approve users, set caps, top up
    operator_audit  — append-only audit log of every operator action
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from livepeer_open_clearinghouse.providers.db import (
    Base,
    TableNameFromClassMixin,
    TimestampMixin,
    UuidPkMixin,
)


class Operator(Base, UuidPkMixin, TimestampMixin, TableNameFromClassMixin):
    """A Livepeer Open Clearinghouse operator. There is no user-to-operator promotion path in MVP.

    `role` is a two-tier RBAC:
      - ``owner`` can do everything, including managing other operators
      - ``member`` can do everything except operator management

    Free-form VARCHAR (not a DB enum) so adding a third role (e.g.
    ``viewer``) doesn't need a migration. Validated at the app layer
    in ``service.create_operator`` / ``service.update_operator``.
    """

    email: Mapped[str] = mapped_column(unique=True, nullable=False)
    name: Mapped[str] = mapped_column(nullable=False)
    token_hash: Mapped[str] = mapped_column(nullable=False)
    role: Mapped[str] = mapped_column(nullable=False, default="member")
    last_login_at: Mapped[datetime | None] = mapped_column(nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(nullable=True)


class OperatorAudit(Base, UuidPkMixin, TimestampMixin, TableNameFromClassMixin):
    """One row per state-mutating operator action."""

    operator_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("operator.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    action: Mapped[str] = mapped_column(nullable=False)
    target_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    params: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
