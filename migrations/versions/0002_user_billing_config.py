"""user_billing_config (per-user cap + auto-replenish overrides)

Per-user override of the four billing knobs that previously only had
operator-wide defaults from Settings. Any column NULL means "use the
global default."

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-22

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user_billing_config",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("spend_period_seconds", sa.Integer(), nullable=True),
        sa.Column("spend_period_cap_wei", sa.Numeric(78, 0), nullable=True),
        sa.Column("auto_replenish_increment_wei", sa.Numeric(78, 0), nullable=True),
        sa.Column("auto_replenish_threshold_wei", sa.Numeric(78, 0), nullable=True),
        sa.Column("updated_by_operator_id", sa.Uuid(), nullable=True),
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
        sa.PrimaryKeyConstraint("user_id", name="pk_user_billing_config"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            name="fk_user_billing_config_user_id_user",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_operator_id"],
            ["operator.id"],
            name="fk_user_billing_config_updated_by_operator_id_operator",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            "spend_period_seconds IS NULL OR spend_period_seconds >= 60",
            name="ck_user_billing_config_min_period",
        ),
    )


def downgrade() -> None:
    op.drop_table("user_billing_config")
