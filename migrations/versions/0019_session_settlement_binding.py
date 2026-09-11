"""Persist the signed broker-session binding and settlement sequence.

Revision ID: 0019
Revises: 0018
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("payment_session", sa.Column("broker_session_id", sa.String(), nullable=True))
    op.add_column(
        "payment_session",
        sa.Column("last_settlement_seq", sa.Integer(), nullable=False, server_default="0"),
    )
    op.alter_column("payment_session", "last_settlement_seq", server_default=None)


def downgrade() -> None:
    op.drop_column("payment_session", "last_settlement_seq")
    op.drop_column("payment_session", "broker_session_id")
