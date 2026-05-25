"""notification_webhook_config

One row per user holding their opt-in webhook URL.

Standard-Webhooks signing on outbound POSTs. Per-user signing secret
is *derived* deterministically as ``HMAC_SHA256(WEBHOOK_SIGNING_SEED,
user_id)`` so we don't need to store ciphertext. The customer sees
the derived secret exactly once at config-creation time; LOC re-
derives it on every send. Operators rotate by changing the seed env
var — that invalidates every customer's webhook secret at once
(v1 trade-off; v2 introduces per-row salts for graceful rotation).

Stored fields:

  ``url``           customer-supplied HTTPS endpoint.
  ``last_test_at``  optional; when the customer triggers a test ping.

Revision ID: 0013
Revises: 0012
Create Date: 2026-05-25

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | Sequence[str] | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notification_webhook_config",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("url", sa.String(length=512), nullable=False),
        sa.Column("last_test_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.PrimaryKeyConstraint("user_id", name="pk_notification_webhook_config"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            name="fk_notification_webhook_config_user_id_user",
            ondelete="CASCADE",
        ),
    )


def downgrade() -> None:
    op.drop_table("notification_webhook_config")
