"""DeclarativeBase, common column types, and mixins.

Every ORM model in the project inherits from `Base`. Mixins provide the
two patterns shared across nearly all tables: a UUID primary key and
`created_at`/`updated_at` timestamps.

Wei amounts use Python `Decimal` mapped to `NUMERIC(78, 0)` via Base's
`type_annotation_map` so any Mapped[Decimal] column lands on the right type.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, MetaData, Numeric, Uuid, func
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column

# A predictable naming convention makes Alembic migrations more deterministic
# and makes constraint references stable.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """The root ORM class for all Livepeer Open Clearinghouse tables."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    # Default type map. Every Mapped[Decimal] becomes NUMERIC(78, 0) so wei
    # amounts always fit. Every Mapped[datetime] becomes a tz-aware
    # TIMESTAMPTZ; our migrations declare all datetime columns with
    # timezone=True so this avoids a mismatch with naive runtime values.
    type_annotation_map = {  # noqa: RUF012
        Decimal: Numeric(78, 0),
        datetime: DateTime(timezone=True),
    }


# ---------------------------------------------------------------------------
# Mixins
# ---------------------------------------------------------------------------


class UuidPkMixin:
    """Adds a UUID primary key column named `id`."""

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)


class TimestampMixin:
    """Adds `created_at` (set on insert) and `updated_at` (set on insert/update)."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class TableNameFromClassMixin:
    """Derive `__tablename__` from the class name (CamelCase -> snake_case)."""

    @declared_attr.directive
    def __tablename__(cls) -> str:  # noqa: N805
        # Convert CamelCase -> snake_case
        name = cls.__name__
        out: list[str] = []
        for i, ch in enumerate(name):
            if ch.isupper() and i > 0 and not name[i - 1].isupper():
                out.append("_")
            out.append(ch.lower())
        return "".join(out)
