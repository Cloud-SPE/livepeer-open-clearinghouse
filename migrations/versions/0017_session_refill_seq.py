"""disambiguate the LOC session refill sequence

Revision ID: 0017
Revises: 0016
Create Date: 2026-08-21
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0017"
down_revision: str | Sequence[str] | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("payment_session", "last_debit_seq", new_column_name="refill_seq")


def downgrade() -> None:
    op.alter_column("payment_session", "refill_seq", new_column_name="last_debit_seq")
