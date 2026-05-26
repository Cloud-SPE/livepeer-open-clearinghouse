"""telemetry_event.broker_operator_id → eth address string

PR-3 reserved ``broker_operator_id`` as ``uuid``. Orchestrators on
the Livepeer registry are identified by their on-chain eth_address,
not by a LOC-side UUID, so we change the column type to VARCHAR(64)
to hold the 0x-prefixed hex. Existing rows are NULL (column was
written but the registry lookup wasn't wired), so the conversion
is trivial.

Revision ID: 0012
Revises: 0011
Create Date: 2026-05-25

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | Sequence[str] | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Use batch_alter_table for SQLite test-fixture compatibility.
    with op.batch_alter_table("telemetry_event") as batch:
        batch.alter_column(
            "broker_operator_id",
            existing_type=sa.Uuid(),
            type_=sa.String(length=64),
            postgresql_using="broker_operator_id::text",
        )


def downgrade() -> None:
    with op.batch_alter_table("telemetry_event") as batch:
        batch.alter_column(
            "broker_operator_id",
            existing_type=sa.String(length=64),
            type_=sa.Uuid(),
            postgresql_using="broker_operator_id::uuid",
        )
