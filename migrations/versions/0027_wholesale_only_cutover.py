"""make wholesale accounting mandatory and reject active legacy work

Revision ID: 0027
Revises: 0026
Create Date: 2026-09-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A legacy engagement cannot be resumed by this application version. Abort
    # the transactional migration before changing defaults if any still need a
    # ticket-era settle, refill, expiry, or recovery operation.
    op.execute(
        """
        DO $$
        DECLARE
          active_sessions bigint;
          active_payments bigint;
        BEGIN
          SELECT COUNT(*) INTO active_sessions
          FROM payment_session
          WHERE accounting_mode = 'legacy_ticket'
            AND state IN ('open', 'draining');

          SELECT COUNT(*) INTO active_payments
          FROM payment AS p
          LEFT JOIN payment_session AS s ON s.id = p.session_id
          WHERE p.status IN ('reserved', 'issued')
            AND (p.session_id IS NULL OR s.state IN ('open', 'draining'));

          IF active_sessions > 0 OR active_payments > 0 THEN
            RAISE EXCEPTION
              'wholesale-only cutover blocked: % active legacy sessions, % active legacy payments',
              active_sessions, active_payments;
          END IF;
        END $$;
        """
    )
    op.alter_column(
        "payment_session",
        "accounting_mode",
        existing_type=sa.String(),
        nullable=False,
        server_default="wholesale_account",
    )


def downgrade() -> None:
    raise RuntimeError("wholesale-only databases cannot be downgraded to legacy issuance")
