"""Tests for the Modules v2 broker settlement query boundary."""

from __future__ import annotations

import base64
import json
import uuid

import httpx
import pytest

from livepeer_open_clearinghouse.providers.broker_settlement import (
    BrokerExchangeOutcome,
    BrokerSettlementQueryError,
    BrokerWholesaleAccountError,
    HttpBrokerSettlementClient,
    NonAdmissionQuery,
)
from tests.fixtures.signed_settlement import (
    signed_job_settlement,
    signed_non_admission,
    signed_session_settlement,
)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_wholesale_account_query_is_strict_and_route_scoped() -> None:
    payer = "0x" + "aa" * 20

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/livepeer/v1/payment/account"
        assert json.loads(request.content) == {"payer_eth_address": payer}
        return httpx.Response(
            200,
            json={
                "payer": payer,
                "payee": "0x" + "11" * 20,
                "chain_id": 42161,
                "denomination": "wei",
                "credited_value_wei": "1000",
                "reserved_value_wei": "300",
                "debited_value_wei": "200",
                "available_value_wei": "500",
                "version": 7,
                "observed_at": "2026-09-09T12:00:00Z",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        result = await HttpBrokerSettlementClient(http_client).get_wholesale_account(
            broker_url="https://broker.example/livepeer/",
            payer_eth_address=payer,
            payee_eth_address="0x" + "11" * 20,
            chain_id=42161,
        )
    assert str(result.available_value_wei) == "500"
    assert result.payee == "0x" + "11" * 20
    assert result.chain_id == 42161


@pytest.mark.unit
@pytest.mark.asyncio
async def test_wholesale_account_query_rejects_changed_payer() -> None:
    transport = httpx.MockTransport(
        lambda _: httpx.Response(
            200,
            json={
                "payer": "0x" + "bb" * 20,
                "payee": "0x" + "11" * 20,
                "chain_id": 42161,
                "denomination": "wei",
                "credited_value_wei": "0",
                "reserved_value_wei": "0",
                "debited_value_wei": "0",
                "available_value_wei": "0",
                "version": 0,
                "observed_at": "2026-09-09T12:00:00Z",
            },
        )
    )
    async with httpx.AsyncClient(transport=transport) as http_client:
        with pytest.raises(BrokerWholesaleAccountError, match="different payer"):
            await HttpBrokerSettlementClient(http_client).get_wholesale_account(
                broker_url="https://broker.example",
                payer_eth_address="0x" + "aa" * 20,
                payee_eth_address="0x" + "11" * 20,
                chain_id=42161,
            )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_spend_authorization_query_is_strict_and_identity_bound() -> None:
    payer = "0x" + "aa" * 20
    authorization_id = "loc-auth:request-1"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/payment/account"
        assert json.loads(request.content) == {
            "payer_eth_address": payer,
            "authorization_id": authorization_id,
        }
        return httpx.Response(
            200,
            json={
                "payer": payer,
                "authorization_id": authorization_id,
                "state": "expired_unused",
                "reserved_value_wei": "0",
                "billed_value_wei": "0",
                "released_value_wei": "0",
                "actual_units": 0,
                "settlement_seq": 0,
                "observed_at": "2026-09-09T12:00:00Z",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        result = await HttpBrokerSettlementClient(http_client).get_spend_authorization(
            broker_url="https://broker.example",
            payer_eth_address=payer,
            authorization_id=authorization_id,
        )
    assert result.state.value == "expired_unused"
    assert result.authorization_id == authorization_id


@pytest.mark.unit
@pytest.mark.asyncio
async def test_wholesale_account_funding_keeps_ticket_inside_loc() -> None:
    payment = b"signed-ticket"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/payment/account/fund"
        assert request.headers["Livepeer-Payment"] == base64.b64encode(payment).decode()
        assert request.headers["Livepeer-Capability"] == "cap"
        assert request.headers["Livepeer-Offering"] == "offer"
        return httpx.Response(
            200,
            json={
                "payer": "0x" + "aa" * 20,
                "payee": "0x" + "11" * 20,
                "credited_value_wei": "25",
                "available_value_wei": "125",
                "account_version": 4,
                "replayed": False,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        result = await HttpBrokerSettlementClient(http_client).fund_wholesale_account(
            broker_url="https://broker.example",
            capability="cap",
            offering="offer",
            payment_bytes=payment,
            payer_eth_address="0x" + "aa" * 20,
            payee_eth_address="0x" + "11" * 20,
            expected_credited_value_wei=25,
        )
    assert str(result.credited_value_wei) == "25"
    assert result.account_version == 4


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


def _encoded_job_settlement(*, request_id: str) -> tuple[str, dict[str, object]]:
    envelope = signed_job_settlement(
        request_id=request_id,
        job_id="broker-job-1",
        work_id="work-1",
        actual_units=7,
        amount_wei=100,
        per_units=1000,
    )
    return base64.b64encode(json.dumps(envelope).encode()).decode(), envelope


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "body", "expected"),
    [
        (202, {"job_id": "j-1", "outcome": "IN_FLIGHT"}, "IN_FLIGHT"),
        (
            202,
            {"job_id": "j-1", "outcome": "ACCOUNTING_PENDING", "debit_attempts": 2},
            "ACCOUNTING_PENDING",
        ),
        (200, {"job_id": "j-1", "outcome": "ADMITTED_OUTCOME_UNKNOWN"}, "ADMITTED_OUTCOME_UNKNOWN"),
        (
            200,
            {"job_id": "j-1", "outcome": "ADMITTED_EVIDENCE_EXPIRED"},
            "ADMITTED_EVIDENCE_EXPIRED",
        ),
        (404, {"outcome": "NO_RECORD"}, "NO_RECORD"),
    ],
)
async def test_job_exchange_parses_normative_outcomes(
    status_code: int, body: dict[str, object], expected: str
) -> None:
    request_id = "loc/request 1"
    body = {"request_id": request_id, **body}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.raw_path == b"/v1/exchange/loc%2Frequest%201"
        return httpx.Response(status_code, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        result = await HttpBrokerSettlementClient(http_client).get_job_exchange(
            broker_url="https://broker.example", request_id=request_id
        )

    assert result.outcome is BrokerExchangeOutcome(expected)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_job_exchange_decodes_signed_settlement() -> None:
    encoded, envelope = _encoded_job_settlement(request_id="request-1")
    transport = httpx.MockTransport(
        lambda _: httpx.Response(
            200,
            headers={"Livepeer-Settlement": encoded},
            json={
                "request_id": "request-1",
                "job_id": "broker-job-1",
                "state": "terminal",
                "outcome": "SETTLED",
                "status": 200,
                "work_units": 7,
                "unit": "token",
                "settlement": encoded,
            },
        )
    )
    async with httpx.AsyncClient(transport=transport) as http_client:
        result = await HttpBrokerSettlementClient(http_client).get_job_exchange(
            broker_url="https://broker.example", request_id="request-1"
        )
    assert result.settlement == envelope


@pytest.mark.unit
@pytest.mark.asyncio
async def test_job_exchange_decodes_non_admission_as_audit_evidence() -> None:
    encoded, envelope = _encoded_job_settlement(request_id="request-1")
    transport = httpx.MockTransport(
        lambda _: httpx.Response(
            200,
            json={
                "request_id": "request-1",
                "outcome": "NOT_ADMITTED",
                "non_admission": encoded,
            },
        )
    )
    async with httpx.AsyncClient(transport=transport) as http_client:
        result = await HttpBrokerSettlementClient(http_client).get_job_exchange(
            broker_url="https://broker.example", request_id="request-1"
        )
    assert result.non_admission == envelope


def _non_admission_query() -> NonAdmissionQuery:
    return NonAdmissionQuery(
        protocol="paid-job/v1",
        work_id="work-1",
        sender="0x" + "aa" * 20,
        recipient="0x" + "11" * 20,
        quote_id="q-1",
        quote_version=1,
        constraint_fingerprint="00" * 32,
        route_fingerprint="11" * 32,
        job_issued_at="2026-05-24T12:00:00+00:00",
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_non_admission_post_sends_scope_and_decodes_signed_record() -> None:
    envelope = signed_non_admission()
    encoded = base64.b64encode(json.dumps(envelope).encode()).decode()

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/non-admission/request-1"
        assert json.loads(request.content) == _non_admission_query().model_dump(mode="json")
        return httpx.Response(
            200,
            headers={"Livepeer-Non-Admission": encoded},
            json={
                "request_id": "request-1",
                "outcome": "NOT_ADMITTED",
                "replayed": False,
                "non_admission": encoded,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        result = await HttpBrokerSettlementClient(http_client).request_non_admission(
            broker_url="https://broker.example",
            request_id="request-1",
            query=_non_admission_query(),
        )
    assert result.outcome is BrokerExchangeOutcome.NOT_ADMITTED
    assert result.non_admission == envelope


@pytest.mark.unit
@pytest.mark.asyncio
async def test_non_admission_post_can_return_admitted_outcome() -> None:
    transport = httpx.MockTransport(
        lambda _: httpx.Response(
            202,
            json={
                "request_id": "request-1",
                "job_id": "broker-job-1",
                "outcome": "ACCOUNTING_PENDING",
            },
        )
    )
    async with httpx.AsyncClient(transport=transport) as http_client:
        result = await HttpBrokerSettlementClient(http_client).request_non_admission(
            broker_url="https://broker.example",
            request_id="request-1",
            query=_non_admission_query(),
        )
    assert result.outcome is BrokerExchangeOutcome.ACCOUNTING_PENDING


@pytest.mark.unit
@pytest.mark.asyncio
async def test_non_admission_post_rejects_header_body_mismatch() -> None:
    envelope = signed_non_admission()
    encoded = base64.b64encode(json.dumps(envelope).encode()).decode()
    transport = httpx.MockTransport(
        lambda _: httpx.Response(
            200,
            headers={"Livepeer-Non-Admission": "different"},
            json={
                "request_id": "request-1",
                "outcome": "NOT_ADMITTED",
                "non_admission": encoded,
            },
        )
    )
    async with httpx.AsyncClient(transport=transport) as http_client:
        with pytest.raises(BrokerSettlementQueryError):
            await HttpBrokerSettlementClient(http_client).request_non_admission(
                broker_url="https://broker.example",
                request_id="request-1",
                query=_non_admission_query(),
            )


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "body"),
    [
        (200, {"request_id": "other", "outcome": "ADMITTED_OUTCOME_UNKNOWN"}),
        (200, {"request_id": "request-1", "outcome": "SETTLED"}),
        (200, {"request_id": "request-1", "outcome": "NO_RECORD"}),
        (404, {"request_id": "request-1", "outcome": "NOT_ADMITTED"}),
        (202, {"request_id": "request-1", "outcome": "IN_FLIGHT", "surprise": True}),
    ],
)
async def test_job_exchange_rejects_inconsistent_protocol_responses(
    status_code: int, body: dict[str, object]
) -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(status_code, json=body))
    async with httpx.AsyncClient(transport=transport) as http_client:
        with pytest.raises(BrokerSettlementQueryError):
            await HttpBrokerSettlementClient(http_client).get_job_exchange(
                broker_url="https://broker.example", request_id="request-1"
            )
