"""ORM models and queries for the api_keys domain.

Tables:
    api_key — per-user API keys; raw key shown once on creation, hash stored
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from livepeer_open_clearinghouse.providers.db import (
    Base,
    TableNameFromClassMixin,
    TimestampMixin,
    UuidPkMixin,
)


class ApiKey(Base, UuidPkMixin, TimestampMixin, TableNameFromClassMixin):
    """A user-owned API key. The raw key is shown once at creation and never again.

    `prefix` is the public identifier shown in dashboards
    (e.g. ``pymth_live_abcd1234``). `hash` is ``sha256(pepper || raw_key)``.
    Lookup goes by `prefix`; the hash is checked with `hmac.compare_digest`.
    """

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True
    )
    prefix: Mapped[str] = mapped_column(unique=True, nullable=False)
    hash: Mapped[str] = mapped_column(nullable=False)
    label: Mapped[str] = mapped_column(nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(nullable=True)
