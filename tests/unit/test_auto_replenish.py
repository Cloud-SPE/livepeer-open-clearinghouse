"""Unit tests for proactive auto-replenish.

We monkeypatch the three primitives `run_auto_replenish` orchestrates
(`resolve_billing_config`, `_ensure_balance_row`, `topup`) so the test
exercises the decision logic without touching a database. The orchestration
is the only thing we own here — the primitives have their own tests.
"""

from __future__ import annotations

import types
import uuid
from decimal import Decimal

import pytest

from pymthouse.domains.billing import service as billing_service
from pymthouse.settings import Settings


def _settings(**overrides: object) -> Settings:
    return Settings(
        app_env="dev",
        database_url="postgresql+asyncpg://x:x@localhost/x",
        admin_bootstrap_token=None,
        **overrides,  # type: ignore[arg-type]
    )


def _resolved(*, increment: int, threshold: int) -> object:
    """Minimal ResolvedBillingConfig stand-in (only the two fields we read)."""
    return types.SimpleNamespace(
        spend_period_seconds=0,
        spend_period_cap_wei=0,
        auto_replenish_increment_wei=increment,
        auto_replenish_threshold_wei=threshold,
    )


class _Session:
    """Fake AsyncSession exposing just the .scalars() call run_auto_replenish makes."""

    def __init__(self, user_ids: list[uuid.UUID]) -> None:
        self._user_ids = user_ids

    async def scalars(self, _stmt: object) -> list[uuid.UUID]:
        return self._user_ids


def _install_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    config_by_user: dict[uuid.UUID, object],
    balance_by_user: dict[uuid.UUID, int],
) -> list[tuple[uuid.UUID, int]]:
    """Install fakes for the three primitives. Returns the calls list."""
    calls: list[tuple[uuid.UUID, int]] = []

    async def fake_resolve(_session, *, user_id, settings):  # type: ignore[no-untyped-def]
        return config_by_user[user_id]

    async def fake_ensure(_session, *, user_id):  # type: ignore[no-untyped-def]
        return types.SimpleNamespace(amount_wei=Decimal(balance_by_user[user_id]))

    async def fake_topup(_session, *, user_id, amount_wei, kind, operator_id):  # type: ignore[no-untyped-def]
        calls.append((user_id, amount_wei))
        # match the real topup return shape (topup, balance) — unused here
        return (None, None)

    monkeypatch.setattr(billing_service, "resolve_billing_config", fake_resolve)
    monkeypatch.setattr(billing_service, "_ensure_balance_row", fake_ensure)
    monkeypatch.setattr(billing_service, "topup", fake_topup)
    return calls


@pytest.mark.unit
async def test_no_users_means_no_replenish(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _Session([])
    calls = _install_fakes(monkeypatch, config_by_user={}, balance_by_user={})
    n = await billing_service.run_auto_replenish(
        session,  # type: ignore[arg-type]
        clock=types.SimpleNamespace(now=lambda: None),  # type: ignore[arg-type]
        settings=_settings(),
    )
    assert n == 0
    assert calls == []


@pytest.mark.unit
async def test_skips_when_increment_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """auto_replenish_increment_wei=0 ⇒ replenish disabled for this user."""
    u = uuid.uuid4()
    session = _Session([u])
    calls = _install_fakes(
        monkeypatch,
        config_by_user={u: _resolved(increment=0, threshold=1000)},
        balance_by_user={u: 0},
    )
    n = await billing_service.run_auto_replenish(
        session,  # type: ignore[arg-type]
        clock=types.SimpleNamespace(now=lambda: None),  # type: ignore[arg-type]
        settings=_settings(),
    )
    assert n == 0
    assert calls == []


@pytest.mark.unit
async def test_skips_when_threshold_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """threshold=0 ⇒ proactive trigger explicitly disabled (reactive-only mode)."""
    u = uuid.uuid4()
    session = _Session([u])
    calls = _install_fakes(
        monkeypatch,
        config_by_user={u: _resolved(increment=500, threshold=0)},
        balance_by_user={u: 0},
    )
    n = await billing_service.run_auto_replenish(
        session,  # type: ignore[arg-type]
        clock=types.SimpleNamespace(now=lambda: None),  # type: ignore[arg-type]
        settings=_settings(),
    )
    assert n == 0
    assert calls == []


@pytest.mark.unit
async def test_skips_when_balance_above_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    u = uuid.uuid4()
    session = _Session([u])
    calls = _install_fakes(
        monkeypatch,
        config_by_user={u: _resolved(increment=500, threshold=1000)},
        balance_by_user={u: 1500},  # already above threshold
    )
    n = await billing_service.run_auto_replenish(
        session,  # type: ignore[arg-type]
        clock=types.SimpleNamespace(now=lambda: None),  # type: ignore[arg-type]
        settings=_settings(),
    )
    assert n == 0
    assert calls == []


@pytest.mark.unit
async def test_fires_when_balance_below_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    u = uuid.uuid4()
    session = _Session([u])
    calls = _install_fakes(
        monkeypatch,
        config_by_user={u: _resolved(increment=500, threshold=1000)},
        balance_by_user={u: 200},  # below threshold
    )
    n = await billing_service.run_auto_replenish(
        session,  # type: ignore[arg-type]
        clock=types.SimpleNamespace(now=lambda: None),  # type: ignore[arg-type]
        settings=_settings(),
    )
    assert n == 1
    assert calls == [(u, 500)]


@pytest.mark.unit
async def test_multiple_users_independent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a = uuid.uuid4()
    b = uuid.uuid4()
    c = uuid.uuid4()
    session = _Session([a, b, c])
    calls = _install_fakes(
        monkeypatch,
        config_by_user={
            a: _resolved(increment=500, threshold=1000),  # fires (low balance)
            b: _resolved(increment=500, threshold=1000),  # skips (above)
            c: _resolved(increment=0, threshold=1000),    # skips (disabled)
        },
        balance_by_user={a: 200, b: 5000, c: 0},
    )
    n = await billing_service.run_auto_replenish(
        session,  # type: ignore[arg-type]
        clock=types.SimpleNamespace(now=lambda: None),  # type: ignore[arg-type]
        settings=_settings(),
    )
    assert n == 1
    assert calls == [(a, 500)]
