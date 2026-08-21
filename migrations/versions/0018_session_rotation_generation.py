"""Track the current paid-session recipient-rotation generation.

Revision ID: 0018
Revises: 0017
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "payment_session",
        sa.Column("rotation_generation", sa.Integer(), nullable=False, server_default="0"),
    )
    op.alter_column("payment_session", "rotation_generation", server_default=None)


def downgrade() -> None:
    op.drop_column("payment_session", "rotation_generation")
