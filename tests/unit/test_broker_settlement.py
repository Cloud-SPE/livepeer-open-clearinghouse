"""Tests for the Modules v2 broker settlement query boundary."""

from __future__ import annotations

import base64
import json
import uuid

import httpx
import pytest

from livepeer_open_clearinghouse.providers.broker_settlement import (
    BrokerSettlementQueryError,
    HttpBrokerSettlementClient,
)
from tests.fixtures.signed_settlement import signed_session_settlement


@pytest.mark.unit
@pytest.mark.asyncio
async def test_query_decodes_settlement_header_by_gateway_session_id() -> None:
    gateway_session_id = uuid.uuid4()
    envelope = signed_session_settlement(gateway_session_id=str(gateway_session_id))

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/livepeer/v1/settlement/{gateway_session_id}"
        encoded = base64.b64encode(json.dumps(envelope).encode()).decode()
        return httpx.Response(200, headers={"Livepeer-Settlement": encoded})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = HttpBrokerSettlementClient(http_client)
        result = await client.get_settlement(
            broker_url="https://broker.example/livepeer/",
            gateway_session_id=gateway_session_id,
        )

    assert result == envelope


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "header"),
    [(200, None), (200, "not-base64"), (409, None), (503, None)],
)
async def test_query_rejects_non_protocol_responses(status_code: int, header: str | None) -> None:
    headers = {} if header is None else {"Livepeer-Settlement": header}
    transport = httpx.MockTransport(lambda _: httpx.Response(status_code, headers=headers))
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = HttpBrokerSettlementClient(http_client)
        with pytest.raises(BrokerSettlementQueryError):
            await client.get_settlement(
                broker_url="https://broker.example",
                gateway_session_id=uuid.uuid4(),
            )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_query_treats_not_found_as_no_record() -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(404))
    async with httpx.AsyncClient(transport=transport) as http_client:
        result = await HttpBrokerSettlementClient(http_client).get_settlement(
            broker_url="https://broker.example",
            gateway_session_id=uuid.uuid4(),
        )
    assert result is None
