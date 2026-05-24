"""notification_config + portal_notification

Foundation for the v1 notification system (exec-plan 002
§"Customer notification preferences"). Two tables:

  - ``notification_config`` — per (user_id, trigger, channel) override
    row. Absence means "use the operator-configured default for this
    trigger/channel pair," so we don't have to backfill rows on signup.

  - ``portal_notification`` — in-portal banner queue. One row per
    fired banner; dismissed_at marks user-acknowledged.

Revision ID: 0010
Revises: 0009
Create Date: 2026-05-24

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | Sequence[str] | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notification_config",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("trigger", sa.String(length=64), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
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
            "user_id", "trigger", "channel", name="pk_notification_config"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            name="fk_notification_config_user_id_user",
            ondelete="CASCADE",
        ),
    )

    op.create_table(
        "portal_notification",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("trigger", sa.String(length=64), nullable=False),
        sa.Column("body", sa.JSON(), nullable=False),
        sa.Column(
            "fired_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name="pk_portal_notification"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            name="fk_portal_notification_user_id_user",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_portal_notification_user_active",
        "portal_notification",
        ["user_id", sa.text("fired_at DESC")],
        postgresql_where=sa.text("dismissed_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_portal_notification_user_active",
        table_name="portal_notification",
    )
    op.drop_table("portal_notification")
    op.drop_table("notification_config")
