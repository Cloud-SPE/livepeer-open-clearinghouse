"""payment_daemon_deposit_snapshot

Append-only ledger of the payment-daemon's TicketBroker deposit/reserve
state as observed by the periodic poller (see
``domains/payments.service.snapshot_deposit``). One row per scheduler
tick (every 5 minutes by default).

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-22

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "payment_daemon_deposit_snapshot",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "taken_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("deposit_wei", sa.Numeric(78, 0), nullable=False),
        sa.Column("reserve_wei", sa.Numeric(78, 0), nullable=False),
        sa.Column("withdraw_round", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_payment_daemon_deposit_snapshot"),
    )
    op.create_index(
        "ix_payment_daemon_deposit_snapshot_taken_at",
        "payment_daemon_deposit_snapshot",
        ["taken_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_payment_daemon_deposit_snapshot_taken_at",
        table_name="payment_daemon_deposit_snapshot",
    )
    op.drop_table("payment_daemon_deposit_snapshot")
