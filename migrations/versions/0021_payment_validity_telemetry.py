"""Persist payer round and ticket-validity telemetry.

Revision ID: 0021
Revises: 0020
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("payment", sa.Column("ticket_validity_period", sa.BigInteger(), nullable=True))
    op.add_column(
        "payment",
        sa.Column("ticket_validity_period_observed_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "payment_daemon_deposit_snapshot",
        sa.Column("current_round", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "payment_daemon_deposit_snapshot",
        sa.Column("ticket_validity_period", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "payment_daemon_deposit_snapshot",
        sa.Column("ticket_validity_period_observed_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("payment_daemon_deposit_snapshot", "ticket_validity_period_observed_at")
    op.drop_column("payment_daemon_deposit_snapshot", "ticket_validity_period")
    op.drop_column("payment_daemon_deposit_snapshot", "current_round")
    op.drop_column("payment", "ticket_validity_period_observed_at")
    op.drop_column("payment", "ticket_validity_period")
