"""Isolate product/environment accounts while retaining legacy credit for audit.

Revision ID: 0029
Revises: 0028
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    pending = bind.scalar(
        sa.text("SELECT count(*) FROM wholesale_funding WHERE status != 'acknowledged'")
    )
    active = bind.scalar(
        sa.text(
            "SELECT count(*) FROM spend_authorization_grant WHERE state NOT IN ('settled', 'expired_unused', 'superseded')"
        )
    )
    if pending or active:
        raise RuntimeError(
            "Drain and reconcile legacy grants and funding before account isolation migration"
        )
    for table in ("wholesale_account", "spend_authorization_grant"):
        op.add_column(
            table,
            sa.Column("wholesale_account_id", sa.String(128), nullable=False, server_default=""),
        )
        op.alter_column(table, "wholesale_account_id", server_default=None)
    # Empty IDs are retained audit rows, never relabeled as a new product account.
    op.drop_constraint("uq_wholesale_account_identity", "wholesale_account", type_="unique")
    op.create_unique_constraint(
        "uq_wholesale_account_identity",
        "wholesale_account",
        [
            "chain_id",
            "payer_eth_address",
            "payee_eth_address",
            "settlement_domain_id",
            "wholesale_account_id",
            "denomination",
        ],
    )


def downgrade() -> None:
    raise RuntimeError("isolated accounts cannot be merged into a wallet-wide account")
