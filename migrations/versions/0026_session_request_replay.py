"""serialize session creation by customer and broker request

Revision ID: 0026
Revises: 0025
Create Date: 2026-09-09
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_payment_session_user_broker_request",
        "payment_session",
        ["user_id", "broker_request_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_payment_session_user_broker_request",
        "payment_session",
        type_="unique",
    )
