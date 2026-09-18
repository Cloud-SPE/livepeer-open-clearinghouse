"""Persist the predecessor for the current session rotation generation.

Revision ID: 0022
Revises: 0021
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "payment_session",
        sa.Column("predecessor_work_id", sa.String(), nullable=True),
    )
    # Existing v2 reactive rotations recorded the rejected generation on the
    # payment row. Backfill once so their terminal settlement remains
    # verifiable after upgrading to the explicit session binding.
    op.execute(
        sa.text(
            """
            UPDATE payment_session
            SET predecessor_work_id = (
                SELECT payment.work_id
                FROM payment
                WHERE payment.session_id = payment_session.id
                  AND payment.status = 'refused'
                  AND payment.refused_reason = 'invalid_recipient_rand'
                ORDER BY payment.created_at DESC
                LIMIT 1
            )
            WHERE payment_session.rotation_generation > 0
            """
        )
    )


def downgrade() -> None:
    op.drop_column("payment_session", "predecessor_work_id")
