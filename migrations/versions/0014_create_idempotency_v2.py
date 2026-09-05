"""create idempotency v2

Revision ID: 0014
Revises: 0013
Create Date: 2026-08-20

The legacy ledger was unused after handoff-mode replaced /payments/mint.
Modules v2 is a breaking cutover, so rebuild it around the job/session
creation contract instead of carrying an ambiguous old key namespace.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | Sequence[str] | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_table("payment_idempotency_key")
    op.create_table(
        "payment_idempotency_key",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("api_key_id", sa.Uuid(), nullable=False),
        sa.Column("operation", sa.String(length=32), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("broker_request_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("response_payload", sa.JSON(), nullable=True),
        sa.Column("payment_id", sa.Uuid(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
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
        sa.PrimaryKeyConstraint(
            "user_id", "operation", "idempotency_key", name="pk_payment_idempotency_key"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            name="fk_payment_idempotency_key_user_id_user",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["api_key_id"],
            ["api_key.id"],
            name="fk_payment_idempotency_key_api_key_id_api_key",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["payment_id"],
            ["payment.id"],
            name="fk_payment_idempotency_key_payment_id_payment",
            ondelete="SET NULL",
        ),
    )


def downgrade() -> None:
    op.drop_table("payment_idempotency_key")
    op.create_table(
        "payment_idempotency_key",
        sa.Column("api_key_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("response_payload", sa.JSON(), nullable=True),
        sa.Column("payment_id", sa.Uuid(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("api_key_id", "idempotency_key", name="pk_payment_idempotency_key"),
        sa.ForeignKeyConstraint(
            ["api_key_id"],
            ["api_key.id"],
            name="fk_payment_idempotency_key_api_key_id_api_key",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["payment_id"],
            ["payment.id"],
            name="fk_payment_idempotency_key_payment_id_payment",
            ondelete="SET NULL",
        ),
    )
