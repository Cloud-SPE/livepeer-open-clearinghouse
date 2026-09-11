"""drop the never-written usage_record table

Revision ID: 0024
Revises: 0023
Create Date: 2026-09-08

``usage_record`` shipped in the initial schema for a per-key usage tally,
but no code path ever wrote to it. Every usage figure is derived from
``payment_session`` (capability, offering, api key, units, funded, billed),
so the table goes: one source of truth.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("usage_record")


def downgrade() -> None:
    op.create_table(
        "usage_record",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("payment_id", sa.Uuid(), nullable=False),
        sa.Column("api_key_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("actual_work_units", sa.BigInteger(), nullable=False),
        sa.Column("actual_cost_wei", sa.Numeric(78, 0), nullable=False),
        sa.Column("request_id", sa.String(), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name="pk_usage_record"),
        sa.ForeignKeyConstraint(
            ["payment_id"], ["payment.id"], name="fk_usage_record_payment_id_payment", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["api_key_id"], ["api_key.id"], name="fk_usage_record_api_key_id_api_key", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["user.id"], name="fk_usage_record_user_id_user", ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("api_key_id", "payment_id", name="uq_usage_record_api_key_payment"),
    )
