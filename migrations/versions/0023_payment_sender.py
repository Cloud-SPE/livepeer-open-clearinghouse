"""Persist the payer sender parsed from each minted envelope.

Revision ID: 0023
Revises: 0022
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Existing envelopes were not persisted, so their sender cannot be
    # reconstructed. New mints always populate this field fail-closed.
    op.add_column("payment", sa.Column("sender_eth_address", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("payment", "sender_eth_address")
