"""Pytest fixtures: spin up the mock LOC + mock broker for each
scenario, hand the runner a configured OpenClearinghouseClient + the
URLs needed to call the inspect APIs."""

from __future__ import annotations

import json
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

# Re-anchor sys.path so `conformance.mock_*` imports work when pytest
# is invoked from the repo root.
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
# Also add the Python SDK so `livepeer_open_clearinghouse_sdk` resolves.
_SDK_SRC = _REPO_ROOT / "sdks" / "python" / "src"
if str(_SDK_SRC) not in sys.path:
    sys.path.insert(0, str(_SDK_SRC))

from conformance.mock_broker.server import (  # noqa: E402
    serve_in_background as serve_broker,
)
from conformance.mock_loc.server import (  # noqa: E402
    serve_in_background as serve_loc,
)

SCENARIOS_DIR = _REPO_ROOT / "conformance" / "scenarios"


def _resolve_broker_url(scenario_dict: dict[str, Any], broker_url: str) -> dict[str, Any]:
    """Replace `{BROKER_URL}` placeholders in the scenario with the
    actual mock-broker URL."""
    text = json.dumps(scenario_dict)
    return json.loads(text.replace("{BROKER_URL}", broker_url))


@pytest.fixture
def scenario(request: pytest.FixtureRequest) -> dict[str, Any]:
    """Each test parametrizes with the scenario file basename."""
    name = request.param
    path = SCENARIOS_DIR / f"{name}.json"
    return json.loads(path.read_text())


@pytest.fixture
def mock_servers(
    scenario: dict[str, Any],
) -> Iterator[tuple[str, str, str]]:
    """Yield ``(loc_url, broker_url, scenario_path_with_broker_subst)``.

    The mocks are spun up in background threads bound to ephemeral
    ports; teardown happens via the context manager exits.
    """
    # Start broker first so we can substitute its URL into the scenario
    # before launching LOC (which references {BROKER_URL} in body).
    with tempfile.TemporaryDirectory() as tmp:
        broker_scenario_path = Path(tmp) / "broker.json"
        # Broker doesn't substitute anything; it can be the raw scenario.
        broker_scenario_path.write_text(json.dumps(scenario))

        with serve_broker(str(broker_scenario_path)) as broker_port:
            broker_url = f"http://127.0.0.1:{broker_port}"
            resolved = _resolve_broker_url(scenario, broker_url)
            loc_scenario_path = Path(tmp) / "loc.json"
            loc_scenario_path.write_text(json.dumps(resolved))
            with serve_loc(str(loc_scenario_path)) as loc_port:
                loc_url = f"http://127.0.0.1:{loc_port}"
                yield loc_url, broker_url, str(loc_scenario_path)


@pytest.fixture
def sdk_client(mock_servers: tuple[str, str, str]):
    """Pre-built OpenClearinghouseClient pointed at the mock LOC."""
    from livepeer_open_clearinghouse_sdk import OpenClearinghouseClient

    loc_url, _broker_url, _ = mock_servers
    client = OpenClearinghouseClient(base_url=loc_url, api_key="pymth_live_conformance")
    yield client


@pytest.fixture
def call_logs(mock_servers: tuple[str, str, str]):
    """Returns a sync helper that fetches the LOC + broker call logs."""
    loc_url, broker_url, _ = mock_servers

    def _fetch() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        with httpx.Client(timeout=5.0) as h:
            loc = h.get(f"{loc_url}/_test/inspect").json()["calls"]
            broker = h.get(f"{broker_url}/_test/inspect").json()["calls"]
        return loc, broker

    return _fetch
