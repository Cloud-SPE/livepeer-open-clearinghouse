"""Unit tests for ResolvedBillingConfig fallback logic.

We don't need a real DB — we build a fake AsyncSession that returns the row
we control. The function under test is essentially a value-fallback algebra
on (per-user override, global default).
"""

from __future__ import annotations

import types
import uuid
from decimal import Decimal

import pytest

from pymthouse.domains.billing import service as billing_service
from pymthouse.domains.billing.repo import UserBillingConfig
from pymthouse.settings import Settings


def _settings(**overrides: object) -> Settings:
    return Settings(
        app_env="dev",
        database_url="postgresql+asyncpg://x:x@localhost/x",
        default_spend_period_seconds=86_400,
        default_spend_period_cap_wei=1_000_000,
        auto_replenish_increment_wei=50_000,
        **overrides,  # type: ignore[arg-type]
    )


class _FakeSession:
    """Minimal AsyncSession stand-in returning a fixed UserBillingConfig row."""

    def __init__(self, row: UserBillingConfig | None) -> None:
        self._row = row

    async def scalar(self, _stmt: object) -> UserBillingConfig | None:
        return self._row


@pytest.mark.unit
async def test_resolve_uses_defaults_when_no_row() -> None:
    s = _FakeSession(None)
    cfg = await billing_service.resolve_billing_config(
        s, user_id=uuid.uuid4(), settings=_settings()  # type: ignore[arg-type]
    )
    assert cfg.spend_period_seconds == 86_400
    assert cfg.spend_period_cap_wei == 1_000_000
    assert cfg.auto_replenish_increment_wei == 50_000
    assert cfg.auto_replenish_threshold_wei == 0


@pytest.mark.unit
async def test_resolve_uses_overrides_when_provided() -> None:
    row = types.SimpleNamespace(
        spend_period_seconds=3_600,
        spend_period_cap_wei=Decimal(200_000),
        auto_replenish_increment_wei=Decimal(10_000),
        auto_replenish_threshold_wei=Decimal(5_000),
    )
    s = _FakeSession(row)  # type: ignore[arg-type]
    cfg = await billing_service.resolve_billing_config(
        s, user_id=uuid.uuid4(), settings=_settings()  # type: ignore[arg-type]
    )
    assert cfg.spend_period_seconds == 3_600
    assert cfg.spend_period_cap_wei == 200_000
    assert cfg.auto_replenish_increment_wei == 10_000
    assert cfg.auto_replenish_threshold_wei == 5_000


@pytest.mark.unit
async def test_resolve_mixes_overrides_with_defaults() -> None:
    row = types.SimpleNamespace(
        spend_period_seconds=None,  # inherit default
        spend_period_cap_wei=Decimal(777),  # override
        auto_replenish_increment_wei=None,  # inherit default
        auto_replenish_threshold_wei=None,  # default-to-zero
    )
    s = _FakeSession(row)  # type: ignore[arg-type]
    cfg = await billing_service.resolve_billing_config(
        s, user_id=uuid.uuid4(), settings=_settings()  # type: ignore[arg-type]
    )
    assert cfg.spend_period_seconds == 86_400
    assert cfg.spend_period_cap_wei == 777
    assert cfg.auto_replenish_increment_wei == 50_000
    assert cfg.auto_replenish_threshold_wei == 0
