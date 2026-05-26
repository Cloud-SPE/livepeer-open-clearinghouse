"""user_billing_config.tier

Adds a free-form ``tier`` VARCHAR column to ``user_billing_config``.
Used as the value of ``account_tier`` in telemetry-event enrichment
(exec-plan 002 §"Operator-only enrichment"). Operators set the
tier per customer through the admin billing-config endpoint; NULL
means "no tier assigned" and tells telemetry to leave the row's
account_tier column NULL.

Revision ID: 0011
Revises: 0010
Create Date: 2026-05-25

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | Sequence[str] | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "user_billing_config",
        sa.Column("tier", sa.String(length=32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("user_billing_config", "tier")
