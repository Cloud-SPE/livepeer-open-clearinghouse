"""email_send + email_event

Records every outbound transactional email (`email_send`) and every
delivery event we receive from the upstream provider (`email_event`).
This is the persistence side of the Resend webhook integration —
the webhook endpoint at ``POST /v1/webhooks/resend`` writes an
``email_event`` row per accepted callback; the sync send path writes
the matching ``email_send`` row.

email_send
  One row per accepted outbound send. ``provider_message_id`` is the
  upstream ID (Resend's ``id`` field on a successful send). This is the
  correlation key webhook events arrive against.

email_event
  Append-only event log. ``provider_event_id`` (Standard Webhooks'
  ``webhook-id``) is the dedup key — webhook deliveries can retry, and
  the unique index prevents double-recording. ``event_type`` is the
  Resend-style ``email.delivered`` / ``email.bounced`` / etc.

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-23

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "email_send",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("to_address", sa.String(), nullable=False),
        sa.Column("subject", sa.String(), nullable=False),
        sa.Column("provider_message_id", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="sent"),
        sa.Column(
            "sent_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
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
        sa.PrimaryKeyConstraint("id", name="pk_email_send"),
    )
    op.create_index(
        "ix_email_send_provider_message_id",
        "email_send",
        ["provider_message_id"],
    )
    op.create_index(
        "ix_email_send_to_address",
        "email_send",
        ["to_address"],
    )
    op.create_index(
        "ix_email_send_sent_at",
        "email_send",
        ["sent_at"],
    )

    op.create_table(
        "email_event",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("provider_event_id", sa.String(), nullable=False),
        sa.Column(
            "email_send_id",
            sa.Uuid(),
            sa.ForeignKey("email_send.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("provider_message_id", sa.String(), nullable=True),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("to_address", sa.String(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
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
        sa.PrimaryKeyConstraint("id", name="pk_email_event"),
        sa.UniqueConstraint("provider_event_id", name="uq_email_event_provider_event_id"),
    )
    op.create_index(
        "ix_email_event_email_send_id",
        "email_event",
        ["email_send_id"],
    )
    op.create_index(
        "ix_email_event_event_type",
        "email_event",
        ["event_type"],
    )
    op.create_index(
        "ix_email_event_received_at",
        "email_event",
        ["received_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_email_event_received_at", table_name="email_event")
    op.drop_index("ix_email_event_event_type", table_name="email_event")
    op.drop_index("ix_email_event_email_send_id", table_name="email_event")
    op.drop_table("email_event")
    op.drop_index("ix_email_send_sent_at", table_name="email_send")
    op.drop_index("ix_email_send_to_address", table_name="email_send")
    op.drop_index("ix_email_send_provider_message_id", table_name="email_send")
    op.drop_table("email_send")
