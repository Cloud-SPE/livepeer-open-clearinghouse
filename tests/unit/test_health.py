"""Smoke test: the app boots and /health responds."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.mark.unit
def test_health_responds_ok(client: TestClient) -> None:
    res = client.get("/health")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert "version" in body
    assert body["env"] == "dev"
