"""Dependency readiness checks used by orchestration probes."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from livepeer_open_clearinghouse.providers.db import (
    EXPECTED_ALEMBIC_REVISION,
    current_alembic_revision,
)
from livepeer_open_clearinghouse.providers.payment_daemon import PaymentDaemonClient
from livepeer_open_clearinghouse.providers.registry_daemon import RegistryClient
from livepeer_open_clearinghouse.providers.telemetry import get_logger

_logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ReadinessResult:
    revision: str | None
    checks: dict[str, bool]

    @property
    def ready(self) -> bool:
        return all(self.checks.values())


async def check_readiness(
    session: AsyncSession,
    payment_daemon: PaymentDaemonClient,
    registry: RegistryClient,
) -> ReadinessResult:
    """Check schema compatibility and both money-path daemon dependencies."""

    checks = {
        "database": False,
        "schema": False,
        "payment_daemon": False,
        "registry_daemon": False,
    }
    try:
        revision = await current_alembic_revision(session)
        checks["database"] = True
        checks["schema"] = revision == EXPECTED_ALEMBIC_REVISION
    except Exception as exc:
        revision = None
        _logger.warning("readiness.database.failed", error=type(exc).__name__)
    try:
        checks["payment_daemon"] = await asyncio.wait_for(payment_daemon.health(), timeout=2.0)
    except Exception as exc:
        _logger.warning("readiness.payment_daemon.failed", error=type(exc).__name__)
    try:
        checks["registry_daemon"] = await asyncio.wait_for(registry.health(), timeout=2.0)
    except Exception as exc:
        _logger.warning("readiness.registry_daemon.failed", error=type(exc).__name__)
    return ReadinessResult(revision=revision, checks=checks)
