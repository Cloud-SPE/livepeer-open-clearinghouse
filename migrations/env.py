"""Alembic environment script.

Loads the database URL from `pymthouse.settings.get_settings()` so we only
configure the connection in one place. Imports every domain's `repo` module
so that all ORM models attach to `Base.metadata` before autogenerate inspects
it.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# Import every domain's repo module so its ORM classes register with
# Base.metadata. Adding a new domain means adding an import here.
from pymthouse.domains.accounts import repo as _accounts_repo  # noqa: F401
from pymthouse.domains.admin import repo as _admin_repo  # noqa: F401
from pymthouse.domains.api_keys import repo as _api_keys_repo  # noqa: F401
from pymthouse.domains.billing import repo as _billing_repo  # noqa: F401
from pymthouse.domains.payments import repo as _payments_repo  # noqa: F401
from pymthouse.domains.usage import repo as _usage_repo  # noqa: F401
from pymthouse.providers.db import Base
from pymthouse.settings import get_settings

# Alembic config object — exposes values from alembic.ini
config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Resolve the database URL at runtime from Settings (env-driven).
config.set_main_option("sqlalchemy.url", get_settings().database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode, emitting SQL without a DB connection."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        render_as_batch=False,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations in 'online' mode using an async engine."""
    cfg_section = config.get_section(config.config_ini_section) or {}
    connectable = async_engine_from_config(
        cfg_section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
