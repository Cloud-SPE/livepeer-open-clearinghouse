"""Shared pytest fixtures.

Domain-specific fixtures live next to their tests; this file holds only
fixtures that genuinely cross multiple test directories.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from pymthouse.main import create_app
from pymthouse.settings import Settings


@pytest.fixture(scope="session")
def test_settings() -> Settings:
    """A Settings object that won't touch real services."""
    return Settings(
        app_env="dev",
        database_url="postgresql+asyncpg://test:test@localhost:5432/test",
    )


@pytest.fixture()
def client(test_settings: Settings) -> Iterator[TestClient]:
    """A synchronous test client against a fresh app instance."""
    app = create_app(test_settings)
    with TestClient(app) as c:
        yield c
