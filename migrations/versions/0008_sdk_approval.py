"""sdk_approval

Operator-managed allow/deprecate list of SDK identities. The SDK sends
a `Livepeer-Open-Clearinghouse-SDK: lang/version/git_sha7` header on
every LOC request; LOC compares it against this table to bucket each
session into ``approved`` / ``deprecated`` / ``blocked`` / ``unknown``
for admin visibility (and, eventually, mint-time gating).

Rows are unique per ``(lang, version, git_sha7)``. The triple is the
public identifier — admin surfaces show all three; the manifest
endpoint publishes them so SDKs can self-check.

Revision ID: 0008
Revises: 0007
Create Date: 2026-05-24

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sdk_approval",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("lang", sa.String(), nullable=False),
        sa.Column("version", sa.String(), nullable=False),
        sa.Column("git_sha7", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("notes", sa.String(), nullable=True),
        sa.Column("added_by_operator_id", sa.Uuid(), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name="pk_sdk_approval"),
        sa.UniqueConstraint(
            "lang", "version", "git_sha7", name="uq_sdk_approval_triple"
        ),
        sa.ForeignKeyConstraint(
            ["added_by_operator_id"],
            ["operator.id"],
            name="fk_sdk_approval_added_by_operator_id_operator",
            ondelete="SET NULL",
        ),
    )
    op.create_index("ix_sdk_approval_status", "sdk_approval", ["status"])


def downgrade() -> None:
    op.drop_index("ix_sdk_approval_status", table_name="sdk_approval")
    op.drop_table("sdk_approval")
