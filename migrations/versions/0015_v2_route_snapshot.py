"""persist Modules v2 route snapshots

Revision ID: 0015
Revises: 0014
Create Date: 2026-08-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | Sequence[str] | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Preserve historical rows for audit, but no runtime path interprets their
    # old value. Every post-cutover create writes a v1 protocol and snapshot.
    op.alter_column("payment_session", "mode", new_column_name="protocol")
    op.add_column("payment_session", sa.Column("route_snapshot", sa.JSON(), nullable=True))
    op.add_column("payment_session", sa.Column("broker_request_id", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("payment_session", "broker_request_id")
    op.drop_column("payment_session", "route_snapshot")
    op.alter_column("payment_session", "protocol", new_column_name="mode")
