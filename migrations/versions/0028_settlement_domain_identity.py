"""scope wholesale accounting to an immutable settlement domain

Revision ID: 0028
Revises: 0027
Create Date: 2026-09-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COMPAT_SETTLEMENT_DOMAIN_ID = "loc.compat.wholesale-account/1.1.0-draft"


def upgrade() -> None:
    op.add_column(
        "wholesale_account",
        sa.Column(
            "settlement_domain_id",
            sa.String(),
            nullable=False,
            server_default=_COMPAT_SETTLEMENT_DOMAIN_ID,
        ),
    )
    op.alter_column("wholesale_account", "settlement_domain_id", server_default=None)
    op.drop_constraint("uq_wholesale_account_identity", "wholesale_account", type_="unique")
    op.create_unique_constraint(
        "uq_wholesale_account_identity",
        "wholesale_account",
        [
            "chain_id",
            "payer_eth_address",
            "payee_eth_address",
            "settlement_domain_id",
            "denomination",
        ],
    )

    op.add_column(
        "spend_authorization_grant",
        sa.Column(
            "settlement_domain_id",
            sa.String(),
            nullable=False,
            server_default=_COMPAT_SETTLEMENT_DOMAIN_ID,
        ),
    )
    op.alter_column("spend_authorization_grant", "settlement_domain_id", server_default=None)


def downgrade() -> None:
    raise RuntimeError("settlement-domain accounting cannot be downgraded to payer-payee identity")
