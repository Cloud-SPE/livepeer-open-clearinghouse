"""Persist payer-daemon v2 mint idempotency keys.

Revision ID: 0016
Revises: 0015
Create Date: 2026-08-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Historical rows predate the daemon contract and have no mint key.
    op.add_column(
        "payment",
        sa.Column("mint_request_id", sa.String(length=128), nullable=True),
    )
    op.create_index(
        "ix_payment_mint_request_id",
        "payment",
        ["mint_request_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_payment_mint_request_id", table_name="payment")
    op.drop_column("payment", "mint_request_id")
