"""Shared pytest fixtures."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from livepeer_open_clearinghouse.main import create_app
from livepeer_open_clearinghouse.settings import Settings


@pytest.fixture(scope="session")
def test_settings() -> Settings:
    """Settings tuned so unit tests don't try to reach real services.

    `admin_bootstrap_token=None` skips the bootstrap-operator seed in the
    lifespan, which would otherwise require a live Postgres.
    """
    return Settings(
        app_env="dev",
        database_url="postgresql+asyncpg://test:test@localhost:5432/test",
        admin_bootstrap_token=None,
    )


@pytest.fixture()
def client(test_settings: Settings) -> Iterator[TestClient]:
    """A synchronous test client against a fresh app instance."""
    app = create_app(test_settings)
    with TestClient(app) as c:
        yield c
