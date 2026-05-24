"""telemetry_event — raw event store for SDK + server telemetry

Foundation for exec-plan 002 §"SDK telemetry (v1)". One row per
emitted event. SDK events arrive via POST /v1/telemetry; server
events are written by LOC's own runtime. Both share this table.

Enrichment columns (`geo_region`, `account_tier`, `broker_operator_id`,
`ingest_node_id`) are added by the schema in this migration but the
ingest-time enrichment writers land in a follow-up PR — populated as
NULL until then.

Indexes are tuned for the v1 query patterns:

  - `(api_key_id, received_ts DESC)` — customer GET /v1/telemetry/events
    paginated lookup.
  - `(event_type, received_ts DESC)` — admin recent-server-event
    roll-ups (e.g. last-24h `server.refill_denied` counts).
  - `(received_ts)` — retention janitor sweep.

Revision ID: 0009
Revises: 0008
Create Date: 2026-05-24

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "telemetry_event",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("api_key_id", sa.Uuid(), nullable=True),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("event_schema_version", sa.Integer(), nullable=False),
        sa.Column("correlation_id", sa.Uuid(), nullable=True),
        sa.Column("client_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "received_ts",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("source", sa.String(length=8), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        # Enrichment — populated at ingest in a follow-up PR.
        sa.Column("geo_region", sa.String(length=32), nullable=True),
        sa.Column("account_tier", sa.String(length=32), nullable=True),
        sa.Column("broker_operator_id", sa.Uuid(), nullable=True),
        sa.Column("ingest_node_id", sa.String(length=64), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name="pk_telemetry_event"),
        sa.ForeignKeyConstraint(
            ["api_key_id"],
            ["api_key.id"],
            name="fk_telemetry_event_api_key_id_api_key",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            name="fk_telemetry_event_user_id_user",
            ondelete="SET NULL",
        ),
    )
    op.create_index(
        "ix_telemetry_event_api_key_received",
        "telemetry_event",
        ["api_key_id", sa.text("received_ts DESC")],
    )
    op.create_index(
        "ix_telemetry_event_type_received",
        "telemetry_event",
        ["event_type", sa.text("received_ts DESC")],
    )
    op.create_index(
        "ix_telemetry_event_received_ts",
        "telemetry_event",
        ["received_ts"],
    )


def downgrade() -> None:
    op.drop_index("ix_telemetry_event_received_ts", table_name="telemetry_event")
    op.drop_index("ix_telemetry_event_type_received", table_name="telemetry_event")
    op.drop_index("ix_telemetry_event_api_key_received", table_name="telemetry_event")
    op.drop_table("telemetry_event")
