"""Smoke test: the app boots and /health responds."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from livepeer_open_clearinghouse import dependencies
from livepeer_open_clearinghouse.providers.db import EXPECTED_ALEMBIC_REVISION


@pytest.mark.unit
def test_health_responds_ok(client: TestClient) -> None:
    res = client.get("/health")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert "version" in body
    assert body["env"] == "dev"


class _HealthyDependency:
    async def health(self) -> bool:
        return True


class _UnhealthyDependency:
    async def health(self) -> bool:
        return False


async def _fake_session():  # type: ignore[no-untyped-def]
    yield object()


@pytest.mark.unit
def test_readiness_requires_schema_and_both_daemons(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = client.app
    app.dependency_overrides[dependencies.get_session] = _fake_session
    app.dependency_overrides[dependencies.get_payment_daemon] = _HealthyDependency
    app.dependency_overrides[dependencies.get_registry] = _HealthyDependency
    revision = AsyncMock(return_value=EXPECTED_ALEMBIC_REVISION)
    monkeypatch.setattr(
        "livepeer_open_clearinghouse.providers.readiness.current_alembic_revision", revision
    )
    try:
        response = client.get("/ready")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert all(response.json()["checks"].values())


@pytest.mark.unit
def test_readiness_fails_closed_on_schema_or_daemon(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = client.app
    app.dependency_overrides[dependencies.get_session] = _fake_session
    app.dependency_overrides[dependencies.get_payment_daemon] = _HealthyDependency
    app.dependency_overrides[dependencies.get_registry] = _UnhealthyDependency
    monkeypatch.setattr(
        "livepeer_open_clearinghouse.providers.readiness.current_alembic_revision",
        AsyncMock(return_value="0013"),
    )
    try:
        response = client.get("/ready")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"
    assert response.json()["checks"] == {
        "database": True,
        "schema": False,
        "payment_daemon": True,
        "registry_daemon": False,
    }
