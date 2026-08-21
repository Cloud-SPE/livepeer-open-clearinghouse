"""Read signed settlement envelopes from a Modules v2 broker."""

from __future__ import annotations

import base64
import binascii
import uuid
from enum import StrEnum
from typing import Any, Protocol
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class BrokerSettlementQueryError(Exception):
    """The broker lookup did not yield a trustworthy protocol response."""


class BrokerExchangeOutcome(StrEnum):
    """Normative paid-job/v1 request-ID lookup outcomes."""

    SETTLED = "SETTLED"
    ACCOUNTING_PENDING = "ACCOUNTING_PENDING"
    IN_FLIGHT = "IN_FLIGHT"
    ADMITTED_OUTCOME_UNKNOWN = "ADMITTED_OUTCOME_UNKNOWN"
    ADMITTED_EVIDENCE_EXPIRED = "ADMITTED_EVIDENCE_EXPIRED"
    NOT_ADMITTED = "NOT_ADMITTED"
    NO_RECORD = "NO_RECORD"


class _SettlementSignature(BaseModel):
    model_config = ConfigDict(extra="forbid")

    algorithm: str = Field(min_length=1)
    canonicalization: str = Field(min_length=1)
    value: str = Field(min_length=1)


class _SettlementEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    payload: dict[str, Any]
    signature: _SettlementSignature


class _ExchangeResponse(BaseModel):
    """Strict parse of the broker's outcome body.

    Most fields are broker hints. Only a verified signed settlement may
    authorize an accounting transition in the jobs service.
    """

    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1)
    outcome: BrokerExchangeOutcome
    job_id: str | None = None
    state: str | None = None
    status: int | None = None
    work_units: int | None = Field(default=None, ge=0)
    unit: str | None = None
    debit_attempts: int | None = Field(default=None, ge=0)
    deadline: str | None = None
    ended_at: str | None = None
    detail: str | None = None
    settlement: str | None = None
    non_admission: str | None = None


class BrokerExchangeResult(BaseModel):
    """Parsed paid-job/v1 lookup result returned to domain services."""

    model_config = ConfigDict(extra="forbid")

    request_id: str
    outcome: BrokerExchangeOutcome
    job_id: str | None = None
    state: str | None = None
    status: int | None = None
    work_units: int | None = None
    unit: str | None = None
    debit_attempts: int | None = None
    deadline: str | None = None
    ended_at: str | None = None
    detail: str | None = None
    settlement: dict[str, Any] | None = None
    non_admission: dict[str, Any] | None = None


class BrokerSettlementClient(Protocol):
    """Boundary used by session reconciliation to query one logical session."""

    async def get_settlement(
        self, *, broker_url: str, gateway_session_id: uuid.UUID
    ) -> dict[str, Any] | None: ...

    async def get_job_exchange(
        self, *, broker_url: str, request_id: str
    ) -> BrokerExchangeResult: ...


class HttpBrokerSettlementClient:
    """HTTP implementation of the Modules v2 settlement lookup contract."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def get_settlement(
        self, *, broker_url: str, gateway_session_id: uuid.UUID
    ) -> dict[str, Any] | None:
        identifier = quote(str(gateway_session_id), safe="")
        url = f"{broker_url.rstrip('/')}/v1/settlement/{identifier}"
        try:
            response = await self._client.get(url)
        except httpx.HTTPError as exc:
            raise BrokerSettlementQueryError("broker settlement query failed") from exc

        if response.status_code == httpx.codes.NOT_FOUND:
            return None
        if response.status_code != httpx.codes.OK:
            raise BrokerSettlementQueryError(
                f"broker settlement query returned HTTP {response.status_code}"
            )

        encoded = response.headers.get("Livepeer-Settlement")
        if encoded is None:
            raise BrokerSettlementQueryError("broker response omitted Livepeer-Settlement")
        try:
            raw = base64.b64decode(encoded, validate=True)
            envelope = _SettlementEnvelope.model_validate_json(raw)
        except (ValueError, binascii.Error, ValidationError) as exc:
            raise BrokerSettlementQueryError("broker returned a malformed settlement") from exc
        return envelope.model_dump(mode="json")

    async def get_job_exchange(self, *, broker_url: str, request_id: str) -> BrokerExchangeResult:
        """Query every broker-side outcome using LOC's own request ID."""

        identifier = quote(request_id, safe="")
        url = f"{broker_url.rstrip('/')}/v1/exchange/{identifier}"
        try:
            response = await self._client.get(url)
        except httpx.HTTPError as exc:
            raise BrokerSettlementQueryError("broker exchange query failed") from exc

        if response.status_code not in (
            httpx.codes.OK,
            httpx.codes.ACCEPTED,
            httpx.codes.NOT_FOUND,
        ):
            raise BrokerSettlementQueryError(
                f"broker exchange query returned HTTP {response.status_code}"
            )
        try:
            body = _ExchangeResponse.model_validate(response.json())
        except (ValueError, ValidationError) as exc:
            raise BrokerSettlementQueryError(
                "broker returned a malformed exchange outcome"
            ) from exc
        if body.request_id != request_id:
            raise BrokerSettlementQueryError("broker returned a different request_id")
        _validate_exchange_status(response.status_code, body.outcome)
        if (
            body.outcome
            not in (
                BrokerExchangeOutcome.NOT_ADMITTED,
                BrokerExchangeOutcome.NO_RECORD,
            )
            and not body.job_id
        ):
            raise BrokerSettlementQueryError("admitted outcome omitted its broker job_id")

        settlement = _decode_exchange_envelope(body.settlement, label="settlement")
        non_admission = _decode_exchange_envelope(body.non_admission, label="non-admission")
        if body.outcome is BrokerExchangeOutcome.SETTLED:
            if settlement is None:
                raise BrokerSettlementQueryError("SETTLED omitted its signed settlement")
            header = response.headers.get("Livepeer-Settlement")
            if header is not None and header != body.settlement:
                raise BrokerSettlementQueryError("settlement header and body disagree")
        elif settlement is not None:
            raise BrokerSettlementQueryError("non-SETTLED outcome carried a settlement")
        if body.outcome is BrokerExchangeOutcome.NOT_ADMITTED:
            if non_admission is None:
                raise BrokerSettlementQueryError("NOT_ADMITTED omitted its signed record")
        elif non_admission is not None:
            raise BrokerSettlementQueryError("non-admission evidence has the wrong outcome")

        values = body.model_dump(exclude={"settlement", "non_admission"})
        return BrokerExchangeResult(
            **values,
            settlement=settlement,
            non_admission=non_admission,
        )


def _validate_exchange_status(status_code: int, outcome: BrokerExchangeOutcome) -> None:
    expected = {
        BrokerExchangeOutcome.ACCOUNTING_PENDING: httpx.codes.ACCEPTED,
        BrokerExchangeOutcome.IN_FLIGHT: httpx.codes.ACCEPTED,
        BrokerExchangeOutcome.NO_RECORD: httpx.codes.NOT_FOUND,
    }.get(outcome, httpx.codes.OK)
    if status_code != expected:
        raise BrokerSettlementQueryError(
            f"outcome {outcome.value} is invalid for HTTP {status_code}"
        )


def _decode_exchange_envelope(encoded: str | None, *, label: str) -> dict[str, Any] | None:
    if encoded is None:
        return None
    try:
        raw = base64.b64decode(encoded, validate=True)
        envelope = _SettlementEnvelope.model_validate_json(raw)
    except (ValueError, binascii.Error, ValidationError) as exc:
        raise BrokerSettlementQueryError(f"broker returned malformed {label} evidence") from exc
    return envelope.model_dump(mode="json")
