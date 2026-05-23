"""operator.role

Adds a `role` column to the `operator` table for two-tier RBAC:

  - ``owner`` — full access, including managing other operators
  - ``member`` — everything *except* operator management

The bootstrap operator (created at gateway startup from
``ADMIN_BOOTSTRAP_TOKEN``) stays ``owner`` — existing rows are
backfilled accordingly. New operators default to ``member`` unless
the creating owner asks otherwise.

Future iterations may subdivide further (e.g. add ``viewer`` for
read-only audit access). The column is a free-form VARCHAR with an
application-level enum check, not a database enum, so adding new
roles doesn't require another migration.

Revision ID: 0006
Revises: 0005
Create Date: 2026-05-23

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "operator",
        sa.Column("role", sa.String(), nullable=False, server_default="owner"),
    )


def downgrade() -> None:
    op.drop_column("operator", "role")
