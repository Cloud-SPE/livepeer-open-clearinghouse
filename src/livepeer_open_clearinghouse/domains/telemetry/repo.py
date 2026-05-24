"""ORM model + queries for the telemetry domain.

Single table ``telemetry_event`` backs both SDK-emitted events
(``source='sdk'``, arriving via ``POST /v1/telemetry``) and
server-emitted events (``source='server'``, written by LOC's own
runtime). The payload column carries the event-type-specific fields
as JSON; only the universal fields are first-class columns.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from livepeer_open_clearinghouse.providers.db import (
    Base,
    TableNameFromClassMixin,
    TimestampMixin,
    UuidPkMixin,
)


class TelemetryEvent(Base, UuidPkMixin, TimestampMixin, TableNameFromClassMixin):
    """One emitted telemetry event."""

    api_key_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("api_key.id", ondelete="SET NULL"), nullable=True
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    event_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    correlation_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    client_ts: Mapped[datetime | None] = mapped_column(nullable=True)
    received_ts: Mapped[datetime] = mapped_column(nullable=False)
    # 'sdk' or 'server' — keeps the two ingestion paths distinguishable
    # without joining against api_key_id (NULL for some server events).
    source: Mapped[str] = mapped_column(String(8), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    # Enrichment — added at ingest in a follow-up PR.
    geo_region: Mapped[str | None] = mapped_column(String(32), nullable=True)
    account_tier: Mapped[str | None] = mapped_column(String(32), nullable=True)
    broker_operator_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    ingest_node_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
