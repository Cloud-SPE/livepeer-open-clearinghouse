"""payment_session + payment_settlement

Foundation for long-running session support (exec-plan 002, PR-1).

Adds two new tables in the sessions domain plus a nullable
`session_id` FK on the existing `payment` table.

`payment_session` is the first-class record of a long-lived
interaction — opened by `POST /v1/sessions`, transitions through
``open`` → ``draining`` → ``closed``, and is reconciled at the end
against the payer-daemon's ledger. For atomic / streaming /
post-settled jobs that don't need a true session, the
``payment_session`` row is still written so admin observability,
the per-session cap, and the reconciliation janitor work uniformly
across cases.

`payment_settlement` is the append-only event log of everything
that happened to a session: refills granted/denied, balance-low
notifications, the final close. The full `SettlementRecord` from
upstream lands in `raw_record` when the broker provides one.

`payment.session_id` lets us walk every ticket-mint row that funded
a given session — typically one row for case (a/b/c), many for
case (d-extensible) where each refill creates a new payment row
tied to the same session.

Revision ID: 0007
Revises: 0006
Create Date: 2026-05-24

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "payment_session",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("api_key_id", sa.Uuid(), nullable=False),
        sa.Column("work_id", sa.String(), nullable=False),
        sa.Column("capability", sa.String(), nullable=False),
        sa.Column("offering", sa.String(), nullable=False),
        sa.Column("mode", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("estimated_units", sa.BigInteger(), nullable=False),
        sa.Column("max_total_units", sa.BigInteger(), nullable=False),
        sa.Column("funded_value_wei", sa.Numeric(78, 0), nullable=False),
        sa.Column("billed_value_wei", sa.Numeric(78, 0), nullable=True),
        sa.Column("actual_units", sa.BigInteger(), nullable=True),
        sa.Column("outcome", sa.String(), nullable=True),
        sa.Column("breakdown", sa.JSON(), nullable=True),
        sa.Column("sdk_identity", sa.String(), nullable=True),
        sa.Column(
            "opened_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "last_debit_seq",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("last_polled_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name="pk_payment_session"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            name="fk_payment_session_user_id_user",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["api_key_id"],
            ["api_key.id"],
            name="fk_payment_session_api_key_id_api_key",
            ondelete="RESTRICT",
        ),
    )
    op.create_index("ix_payment_session_user_id", "payment_session", ["user_id"])
    op.create_index("ix_payment_session_api_key_id", "payment_session", ["api_key_id"])
    op.create_index("ix_payment_session_work_id", "payment_session", ["work_id"])
    op.create_index("ix_payment_session_state", "payment_session", ["state"])
    # Janitor query: open sessions ordered by last_polled_at (oldest first).
    op.create_index(
        "ix_payment_session_state_last_polled_at",
        "payment_session",
        ["state", "last_polled_at"],
    )

    op.create_table(
        "payment_settlement",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("actual_units", sa.BigInteger(), nullable=True),
        sa.Column("billed_value_wei", sa.Numeric(78, 0), nullable=True),
        sa.Column("outcome", sa.String(), nullable=True),
        sa.Column("raw_record", sa.JSON(), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name="pk_payment_settlement"),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["payment_session.id"],
            name="fk_payment_settlement_session_id_payment_session",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_payment_settlement_session_id",
        "payment_settlement",
        ["session_id"],
    )

    op.add_column(
        "payment",
        sa.Column("session_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_payment_session_id_payment_session",
        source_table="payment",
        referent_table="payment_session",
        local_cols=["session_id"],
        remote_cols=["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_payment_session_id", "payment", ["session_id"])


def downgrade() -> None:
    op.drop_index("ix_payment_session_id", table_name="payment")
    op.drop_constraint(
        "fk_payment_session_id_payment_session",
        "payment",
        type_="foreignkey",
    )
    op.drop_column("payment", "session_id")

    op.drop_index("ix_payment_settlement_session_id", table_name="payment_settlement")
    op.drop_table("payment_settlement")

    op.drop_index(
        "ix_payment_session_state_last_polled_at",
        table_name="payment_session",
    )
    op.drop_index("ix_payment_session_state", table_name="payment_session")
    op.drop_index("ix_payment_session_work_id", table_name="payment_session")
    op.drop_index("ix_payment_session_api_key_id", table_name="payment_session")
    op.drop_index("ix_payment_session_user_id", table_name="payment_session")
    op.drop_table("payment_session")
