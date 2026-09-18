"""separate customer engagement and wholesale account persistence

Revision ID: 0025
Revises: 0024
Create Date: 2026-09-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "payment_session",
        sa.Column(
            "accounting_mode",
            sa.String(),
            nullable=False,
            server_default="legacy_ticket",
        ),
    )
    op.add_column("payment_session", sa.Column("customer_pricing", sa.JSON(), nullable=True))
    op.add_column(
        "payment_session",
        sa.Column("customer_max_debit_wei", sa.Numeric(78, 0), nullable=True),
    )
    op.add_column("payment_session", sa.Column("authorization_id", sa.String(), nullable=True))
    op.create_unique_constraint(
        "uq_payment_session_authorization_id", "payment_session", ["authorization_id"]
    )
    op.add_column("credit_ledger", sa.Column("related_engagement_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_credit_ledger_related_engagement_id_payment_session",
        "credit_ledger",
        "payment_session",
        ["related_engagement_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_unique_constraint(
        "uq_credit_ledger_engagement_reason",
        "credit_ledger",
        ["related_engagement_id", "reason"],
    )

    op.create_table(
        "wholesale_account",
        sa.Column("chain_id", sa.BigInteger(), nullable=False),
        sa.Column("payer_eth_address", sa.String(length=42), nullable=False),
        sa.Column("payee_eth_address", sa.String(length=42), nullable=False),
        sa.Column("denomination", sa.String(length=16), nullable=False),
        sa.Column("protocol_version", sa.String(length=64), nullable=False),
        sa.Column("broker_url", sa.String(), nullable=False),
        sa.Column("credited_value_wei", sa.Numeric(78, 0), nullable=False),
        sa.Column("reserved_value_wei", sa.Numeric(78, 0), nullable=False),
        sa.Column("debited_value_wei", sa.Numeric(78, 0), nullable=False),
        sa.Column("available_value_wei", sa.Numeric(78, 0), nullable=False),
        sa.Column("remote_version", sa.BigInteger(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_wholesale_account"),
        sa.UniqueConstraint(
            "chain_id",
            "payer_eth_address",
            "payee_eth_address",
            "denomination",
            name="uq_wholesale_account_identity",
        ),
    )
    op.create_table(
        "wholesale_exposure_budget",
        sa.Column("scope", sa.String(length=32), nullable=False),
        sa.Column("projected_available_wei", sa.Numeric(78, 0), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("scope", name="pk_wholesale_exposure_budget"),
    )
    op.bulk_insert(
        sa.table(
            "wholesale_exposure_budget",
            sa.column("scope", sa.String()),
            sa.column("projected_available_wei", sa.Numeric(78, 0)),
        ),
        [{"scope": "global", "projected_available_wei": 0}],
    )
    op.create_table(
        "wholesale_funding",
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("mint_request_id", sa.String(length=128), nullable=False),
        sa.Column("correlation_id", sa.String(length=128), nullable=True),
        sa.Column("target_available_wei", sa.Numeric(78, 0), nullable=False),
        sa.Column("observed_available_wei", sa.Numeric(78, 0), nullable=False),
        sa.Column("requested_shortfall_wei", sa.Numeric(78, 0), nullable=False),
        sa.Column("route_snapshot", sa.JSON(), nullable=False),
        sa.Column("payment_bytes", sa.LargeBinary(), nullable=True),
        sa.Column("minted_expected_value_wei", sa.Numeric(78, 0), nullable=False),
        sa.Column("credited_value_wei", sa.Numeric(78, 0), nullable=True),
        sa.Column("work_id", sa.String(), nullable=True),
        sa.Column("account_version", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["wholesale_account.id"],
            name="fk_wholesale_funding_account_id_wholesale_account",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_wholesale_funding"),
        sa.UniqueConstraint("mint_request_id", name="uq_wholesale_funding_mint_request_id"),
    )
    op.create_index("ix_wholesale_funding_account_id", "wholesale_funding", ["account_id"])
    op.create_index("ix_wholesale_funding_correlation_id", "wholesale_funding", ["correlation_id"])
    op.create_table(
        "spend_authorization_grant",
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("authorization_id", sa.String(length=128), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("predecessor_authorization_id", sa.String(length=128), nullable=True),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column("caller_public_key", sa.String(), nullable=False),
        sa.Column("authorization_bytes", sa.LargeBinary(), nullable=False),
        sa.Column("protocol", sa.String(), nullable=False),
        sa.Column("route_snapshot", sa.JSON(), nullable=False),
        sa.Column("payer_eth_address", sa.String(length=42), nullable=False),
        sa.Column("chain_id", sa.BigInteger(), nullable=False),
        sa.Column("denomination", sa.String(length=16), nullable=False),
        sa.Column("max_debit_wei", sa.Numeric(78, 0), nullable=False),
        sa.Column("max_total_units", sa.BigInteger(), nullable=False),
        sa.Column("not_before", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["payment_session.id"],
            name="fk_spend_authorization_grant_session_id_payment_session",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_spend_authorization_grant"),
        sa.UniqueConstraint(
            "authorization_id", name="uq_spend_authorization_grant_authorization_id"
        ),
        sa.UniqueConstraint(
            "session_id",
            "revision",
            name="uq_spend_authorization_grant_session_revision",
        ),
    )
    op.create_index(
        "ix_spend_authorization_grant_session_id",
        "spend_authorization_grant",
        ["session_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_spend_authorization_grant_session_id",
        table_name="spend_authorization_grant",
    )
    op.drop_table("spend_authorization_grant")
    op.drop_index("ix_wholesale_funding_correlation_id", table_name="wholesale_funding")
    op.drop_index("ix_wholesale_funding_account_id", table_name="wholesale_funding")
    op.drop_table("wholesale_funding")
    op.drop_table("wholesale_exposure_budget")
    op.drop_table("wholesale_account")
    op.drop_constraint("uq_credit_ledger_engagement_reason", "credit_ledger", type_="unique")
    op.drop_constraint(
        "fk_credit_ledger_related_engagement_id_payment_session",
        "credit_ledger",
        type_="foreignkey",
    )
    op.drop_column("credit_ledger", "related_engagement_id")
    op.drop_constraint("uq_payment_session_authorization_id", "payment_session", type_="unique")
    op.drop_column("payment_session", "authorization_id")
    op.drop_column("payment_session", "customer_max_debit_wei")
    op.drop_column("payment_session", "customer_pricing")
    op.drop_column("payment_session", "accounting_mode")
