"""user_password_reset (single-use, hashed reset tokens)

Mirrors user_email_verification's shape: token hash stored (never the raw
token), per-row TTL, consumed_at flips on use. Tokens are 1-hour-lived
by default (the TTL is enforced in the service layer, not the schema).

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-22

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user_password_reset",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name="pk_user_password_reset"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            name="fk_user_password_reset_user_id_user",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_user_password_reset_user_id",
        "user_password_reset",
        ["user_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_user_password_reset_user_id", table_name="user_password_reset"
    )
    op.drop_table("user_password_reset")
