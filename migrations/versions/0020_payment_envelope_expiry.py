"""Persist the chain-enforced lifetime of minted payment envelopes.

Revision ID: 0020
Revises: 0019
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("payment", sa.Column("creation_round", sa.BigInteger(), nullable=True))
    op.add_column("payment", sa.Column("expires_after_round", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("payment", "expires_after_round")
    op.drop_column("payment", "creation_round")
