"""initial schema

Creates every table required for the MVP domains: accounts, api_keys,
billing, payments, usage, admin. Discovery has no tables (pass-through).

Revision ID: 0001
Revises:
Create Date: 2026-05-22

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ----- user (root) -----
    op.create_table(
        "user",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("password_hash", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_user"),
        sa.UniqueConstraint("email", name="uq_user_email"),
    )

    # ----- operator (root) -----
    op.create_table(
        "operator",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_operator"),
        sa.UniqueConstraint("email", name="uq_operator_email"),
    )

    # ----- user_email_verification -----
    op.create_table(
        "user_email_verification",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_user_email_verification"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            name="fk_user_email_verification_user_id_user",
            ondelete="CASCADE",
        ),
    )

    # ----- user_oauth_identity -----
    op.create_table(
        "user_oauth_identity",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("provider_user_id", sa.String(), nullable=False),
        sa.Column("email_at_link", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_user_oauth_identity"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            name="fk_user_oauth_identity_user_id_user",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "provider",
            "provider_user_id",
            name="uq_user_oauth_identity_provider_uid",
        ),
    )

    # ----- user_session -----
    op.create_table(
        "user_session",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_user_session"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            name="fk_user_session_user_id_user",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("token_hash", name="uq_user_session_token_hash"),
    )

    # ----- operator_approval -----
    op.create_table(
        "operator_approval",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("operator_id", sa.Uuid(), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revocation_reason", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_operator_approval"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            name="fk_operator_approval_user_id_user",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["operator_id"],
            ["operator.id"],
            name="fk_operator_approval_operator_id_operator",
            ondelete="RESTRICT",
        ),
    )
    # One active approval per user.
    op.create_index(
        "uq_operator_approval_active_per_user",
        "operator_approval",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )

    # ----- api_key -----
    op.create_table(
        "api_key",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("prefix", sa.String(), nullable=False),
        sa.Column("hash", sa.String(), nullable=False),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_api_key"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            name="fk_api_key_user_id_user",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("prefix", name="uq_api_key_prefix"),
    )
    op.create_index("ix_api_key_user_id", "api_key", ["user_id"])

    # ----- credit_balance -----
    op.create_table(
        "credit_balance",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("amount_wei", sa.Numeric(78, 0), nullable=False),
        sa.Column(
            "version", sa.BigInteger(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("user_id", name="pk_credit_balance"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            name="fk_credit_balance_user_id_user",
            ondelete="CASCADE",
        ),
    )

    # ----- credit_topup -----
    op.create_table(
        "credit_topup",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("amount_wei", sa.Numeric(78, 0), nullable=False),
        sa.Column("topup_kind", sa.String(), nullable=False),
        sa.Column("operator_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_credit_topup"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            name="fk_credit_topup_user_id_user",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["operator_id"],
            ["operator.id"],
            name="fk_credit_topup_operator_id_operator",
            ondelete="SET NULL",
        ),
    )
    op.create_index("ix_credit_topup_user_id", "credit_topup", ["user_id"])

    # ----- payment -----
    op.create_table(
        "payment",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("api_key_id", sa.Uuid(), nullable=False),
        sa.Column("work_id", sa.String(), nullable=False),
        sa.Column("recipient_eth_address", sa.String(), nullable=False),
        sa.Column("capability", sa.String(), nullable=False),
        sa.Column("offering", sa.String(), nullable=False),
        sa.Column("work_units_requested", sa.BigInteger(), nullable=False),
        sa.Column("price_per_work_unit_wei", sa.Numeric(78, 0), nullable=False),
        sa.Column("funded_value_wei", sa.Numeric(78, 0), nullable=False),
        sa.Column("expected_value_wei", sa.Numeric(78, 0), nullable=False),
        sa.Column("reserved_wei", sa.Numeric(78, 0), nullable=False),
        sa.Column(
            "refunded_wei",
            sa.Numeric(78, 0),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("refused_reason", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_payment"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            name="fk_payment_user_id_user",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["api_key_id"],
            ["api_key.id"],
            name="fk_payment_api_key_id_api_key",
            ondelete="RESTRICT",
        ),
    )
    op.create_index("ix_payment_user_id", "payment", ["user_id"])
    op.create_index("ix_payment_api_key_id", "payment", ["api_key_id"])
    op.create_index("ix_payment_work_id", "payment", ["work_id"])

    # ----- credit_ledger -----
    op.create_table(
        "credit_ledger",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("delta_wei", sa.Numeric(78, 0), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("related_payment_id", sa.Uuid(), nullable=True),
        sa.Column("related_topup_id", sa.Uuid(), nullable=True),
        sa.Column("created_by_operator_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_credit_ledger"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            name="fk_credit_ledger_user_id_user",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["related_payment_id"],
            ["payment.id"],
            name="fk_credit_ledger_related_payment_id_payment",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["related_topup_id"],
            ["credit_topup.id"],
            name="fk_credit_ledger_related_topup_id_credit_topup",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_operator_id"],
            ["operator.id"],
            name="fk_credit_ledger_created_by_operator_id_operator",
            ondelete="SET NULL",
        ),
    )
    op.create_index("ix_credit_ledger_user_id", "credit_ledger", ["user_id"])

    # ----- payment_idempotency_key -----
    op.create_table(
        "payment_idempotency_key",
        sa.Column("api_key_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("response_payload", sa.JSON(), nullable=True),
        sa.Column("payment_id", sa.Uuid(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint(
            "api_key_id", "idempotency_key", name="pk_payment_idempotency_key"
        ),
        sa.ForeignKeyConstraint(
            ["api_key_id"],
            ["api_key.id"],
            name="fk_payment_idempotency_key_api_key_id_api_key",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["payment_id"],
            ["payment.id"],
            name="fk_payment_idempotency_key_payment_id_payment",
            ondelete="SET NULL",
        ),
    )

    # ----- spend_window -----
    op.create_table(
        "spend_window",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("spent_wei", sa.Numeric(78, 0), nullable=False),
        sa.Column("cap_wei", sa.Numeric(78, 0), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("user_id", "window_start", name="pk_spend_window"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            name="fk_spend_window_user_id_user",
            ondelete="CASCADE",
        ),
    )

    # ----- usage_record -----
    op.create_table(
        "usage_record",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("payment_id", sa.Uuid(), nullable=False),
        sa.Column("api_key_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("actual_work_units", sa.BigInteger(), nullable=False),
        sa.Column("actual_cost_wei", sa.Numeric(78, 0), nullable=False),
        sa.Column("request_id", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_usage_record"),
        sa.ForeignKeyConstraint(
            ["payment_id"],
            ["payment.id"],
            name="fk_usage_record_payment_id_payment",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["api_key_id"],
            ["api_key.id"],
            name="fk_usage_record_api_key_id_api_key",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            name="fk_usage_record_user_id_user",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "api_key_id", "payment_id", name="uq_usage_record_api_key_payment"
        ),
    )

    # ----- operator_audit -----
    op.create_table(
        "operator_audit",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("operator_id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("target_user_id", sa.Uuid(), nullable=True),
        sa.Column("params", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_operator_audit"),
        sa.ForeignKeyConstraint(
            ["operator_id"],
            ["operator.id"],
            name="fk_operator_audit_operator_id_operator",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["target_user_id"],
            ["user.id"],
            name="fk_operator_audit_target_user_id_user",
            ondelete="SET NULL",
        ),
    )
    op.create_index("ix_operator_audit_operator_id", "operator_audit", ["operator_id"])


def downgrade() -> None:
    # Drop in reverse dependency order.
    op.drop_index("ix_operator_audit_operator_id", table_name="operator_audit")
    op.drop_table("operator_audit")
    op.drop_table("usage_record")
    op.drop_table("spend_window")
    op.drop_table("payment_idempotency_key")
    op.drop_index("ix_credit_ledger_user_id", table_name="credit_ledger")
    op.drop_table("credit_ledger")
    op.drop_index("ix_payment_work_id", table_name="payment")
    op.drop_index("ix_payment_api_key_id", table_name="payment")
    op.drop_index("ix_payment_user_id", table_name="payment")
    op.drop_table("payment")
    op.drop_index("ix_credit_topup_user_id", table_name="credit_topup")
    op.drop_table("credit_topup")
    op.drop_table("credit_balance")
    op.drop_index("ix_api_key_user_id", table_name="api_key")
    op.drop_table("api_key")
    op.drop_index(
        "uq_operator_approval_active_per_user", table_name="operator_approval"
    )
    op.drop_table("operator_approval")
    op.drop_table("user_session")
    op.drop_table("user_oauth_identity")
    op.drop_table("user_email_verification")
    op.drop_table("operator")
    op.drop_table("user")
