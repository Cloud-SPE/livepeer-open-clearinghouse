"""Read signed settlement envelopes from a Modules v2 broker."""

from __future__ import annotations

import base64
import binascii
import uuid
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol
from urllib.parse import quote

import httpx
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError


class BrokerSettlementQueryError(Exception):
    """The broker lookup did not yield a trustworthy protocol response."""


class BrokerWholesaleAccountError(Exception):
    """The broker account endpoint failed or returned an untrusted response."""


class WholesaleAccountObservation(BaseModel):
    """Strict broker view of LOC's shared account at one selected payee."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    payer: str = Field(pattern=r"^0x[0-9a-f]{40}$")
    payee: str = Field(pattern=r"^0x[0-9a-f]{40}$")
    chain_id: int = Field(gt=0)
    denomination: str
    credited_value_wei: Decimal = Field(ge=0)
    reserved_value_wei: Decimal = Field(ge=0)
    debited_value_wei: Decimal = Field(ge=0)
    available_value_wei: Decimal = Field(ge=0)
    version: int = Field(ge=0)
    observed_at: AwareDatetime


class WholesaleFundingResult(BaseModel):
    """Strict acknowledgement from the funding-only broker endpoint."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    payer: str = Field(pattern=r"^0x[0-9a-f]{40}$")
    payee: str = Field(pattern=r"^0x[0-9a-f]{40}$")
    credited_value_wei: Decimal = Field(ge=0)
    available_value_wei: Decimal = Field(ge=0)
    account_version: int = Field(ge=0)
    replayed: bool


class SpendAuthorizationState(StrEnum):
    ISSUED = "issued"
    ADMITTED = "admitted"
    SETTLED = "settled"
    EXPIRED_UNUSED = "expired_unused"
    OUTCOME_UNKNOWN = "outcome_unknown"
    SUPERSEDED = "superseded"


class SpendAuthorizationObservation(BaseModel):
    """Strict durable receiver state for one route-locked authorization."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    payer: str = Field(pattern=r"^0x[0-9a-f]{40}$")
    authorization_id: str = Field(min_length=1)
    state: SpendAuthorizationState
    reserved_value_wei: Decimal = Field(ge=0)
    billed_value_wei: Decimal = Field(ge=0)
    released_value_wei: Decimal = Field(ge=0)
    actual_units: int = Field(ge=0)
    settlement_seq: int = Field(ge=0)
    observed_at: AwareDatetime


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
    observed_at: str | None = None
    coverage_started_at: str | None = None
    replayed: bool | None = None


class NonAdmissionQuery(BaseModel):
    """Strict caller-owned scope for a broker non-admission assertion."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol: str = Field(min_length=1)
    work_id: str = Field(min_length=1)
    sender: str = Field(pattern=r"^0x[0-9a-f]{40}$")
    recipient: str = Field(pattern=r"^0x[0-9a-f]{40}$")
    quote_id: str = Field(min_length=1)
    quote_version: int = Field(ge=1)
    constraint_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    route_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    job_issued_at: str = Field(min_length=1)


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
    observed_at: str | None = None
    coverage_started_at: str | None = None
    replayed: bool | None = None
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

    async def request_non_admission(
        self, *, broker_url: str, request_id: str, query: NonAdmissionQuery
    ) -> BrokerExchangeResult: ...


class BrokerWholesaleAccountClient(Protocol):
    """Boundary for observing and funding stable payer-payee credit."""

    async def get_wholesale_account(
        self,
        *,
        broker_url: str,
        payer_eth_address: str,
        payee_eth_address: str,
        chain_id: int,
    ) -> WholesaleAccountObservation: ...

    async def fund_wholesale_account(
        self,
        *,
        broker_url: str,
        capability: str,
        offering: str,
        payment_bytes: bytes,
        payer_eth_address: str,
        payee_eth_address: str,
        expected_credited_value_wei: Decimal,
    ) -> WholesaleFundingResult: ...

    async def get_spend_authorization(
        self,
        *,
        broker_url: str,
        payer_eth_address: str,
        authorization_id: str,
    ) -> SpendAuthorizationObservation: ...


class HttpBrokerSettlementClient:
    """HTTP implementation of the Modules v2 settlement lookup contract."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def get_wholesale_account(
        self,
        *,
        broker_url: str,
        payer_eth_address: str,
        payee_eth_address: str,
        chain_id: int,
    ) -> WholesaleAccountObservation:
        """Read one TLS-bound account snapshot for shortfall calculation."""

        url = f"{broker_url.rstrip('/')}/v1/payment/account"
        try:
            response = await self._client.post(url, json={"payer_eth_address": payer_eth_address})
        except httpx.HTTPError as exc:
            raise BrokerWholesaleAccountError("broker account query failed") from exc
        if response.status_code != httpx.codes.OK:
            raise BrokerWholesaleAccountError(
                f"broker account query returned HTTP {response.status_code}"
            )
        try:
            account = WholesaleAccountObservation.model_validate(response.json())
        except (ValueError, ValidationError) as exc:
            raise BrokerWholesaleAccountError(
                "broker returned a malformed wholesale account"
            ) from exc
        if account.payer != payer_eth_address.lower():
            raise BrokerWholesaleAccountError("broker returned a different payer")
        if account.payee != payee_eth_address.lower():
            raise BrokerWholesaleAccountError("broker returned a different payee")
        if account.chain_id != chain_id:
            raise BrokerWholesaleAccountError("broker returned a different chain_id")
        if account.denomination != "wei":
            raise BrokerWholesaleAccountError("broker returned a non-wei account")
        return account

    async def get_spend_authorization(
        self,
        *,
        broker_url: str,
        payer_eth_address: str,
        authorization_id: str,
    ) -> SpendAuthorizationObservation:
        """Read irrevocable authorization state from its locked broker."""

        url = f"{broker_url.rstrip('/')}/v1/payment/account"
        body = {
            "payer_eth_address": payer_eth_address,
            "authorization_id": authorization_id,
        }
        try:
            response = await self._client.post(url, json=body)
        except httpx.HTTPError as exc:
            raise BrokerWholesaleAccountError("broker authorization query failed") from exc
        if response.status_code != httpx.codes.OK:
            raise BrokerWholesaleAccountError(
                f"broker authorization query returned HTTP {response.status_code}"
            )
        try:
            observation = SpendAuthorizationObservation.model_validate(response.json())
        except (ValueError, ValidationError) as exc:
            raise BrokerWholesaleAccountError(
                "broker returned a malformed spend authorization"
            ) from exc
        if observation.payer != payer_eth_address.lower():
            raise BrokerWholesaleAccountError("broker returned a different authorization payer")
        if observation.authorization_id != authorization_id:
            raise BrokerWholesaleAccountError("broker returned a different authorization")
        return observation

    async def fund_wholesale_account(
        self,
        *,
        broker_url: str,
        capability: str,
        offering: str,
        payment_bytes: bytes,
        payer_eth_address: str,
        payee_eth_address: str,
        expected_credited_value_wei: Decimal,
    ) -> WholesaleFundingResult:
        """Deposit an envelope without delegating it to an end caller."""

        if not payment_bytes:
            raise BrokerWholesaleAccountError("cannot fund an account with an empty payment")
        url = f"{broker_url.rstrip('/')}/v1/payment/account/fund"
        headers = {
            "Livepeer-Payment": base64.b64encode(payment_bytes).decode("ascii"),
            "Livepeer-Capability": capability,
            "Livepeer-Offering": offering,
        }
        try:
            response = await self._client.post(url, headers=headers, content=b"")
        except httpx.HTTPError as exc:
            raise BrokerWholesaleAccountError("broker account funding failed") from exc
        if response.status_code != httpx.codes.OK:
            raise BrokerWholesaleAccountError(
                f"broker account funding returned HTTP {response.status_code}"
            )
        try:
            result = WholesaleFundingResult.model_validate(response.json())
        except (ValueError, ValidationError) as exc:
            raise BrokerWholesaleAccountError(
                "broker returned a malformed account funding acknowledgement"
            ) from exc
        if result.payer != payer_eth_address.lower():
            raise BrokerWholesaleAccountError("broker funded a different payer")
        if result.payee != payee_eth_address.lower():
            raise BrokerWholesaleAccountError("broker funded a different payee")
        if result.credited_value_wei != expected_credited_value_wei:
            raise BrokerWholesaleAccountError("broker credited an unexpected value")
        return result

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
        return _parse_exchange_response(response, request_id=request_id, allow_no_record=True)

    async def request_non_admission(
        self, *, broker_url: str, request_id: str, query: NonAdmissionQuery
    ) -> BrokerExchangeResult:
        """Ask the broker to issue or replay attributable absence evidence."""

        identifier = quote(request_id, safe="")
        url = f"{broker_url.rstrip('/')}/v1/non-admission/{identifier}"
        try:
            response = await self._client.post(url, json=query.model_dump(mode="json"))
        except httpx.HTTPError as exc:
            raise BrokerSettlementQueryError("broker non-admission query failed") from exc
        if response.status_code not in (
            httpx.codes.OK,
            httpx.codes.ACCEPTED,
            httpx.codes.CONFLICT,
        ):
            raise BrokerSettlementQueryError(
                f"broker non-admission query returned HTTP {response.status_code}"
            )
        return _parse_exchange_response(response, request_id=request_id, allow_no_record=False)


def _parse_exchange_response(
    response: httpx.Response, *, request_id: str, allow_no_record: bool
) -> BrokerExchangeResult:
    try:
        body = _ExchangeResponse.model_validate(response.json())
    except (ValueError, ValidationError) as exc:
        raise BrokerSettlementQueryError("broker returned a malformed exchange outcome") from exc
    if body.request_id != request_id:
        raise BrokerSettlementQueryError("broker returned a different request_id")
    if not allow_no_record and body.outcome is BrokerExchangeOutcome.NO_RECORD:
        raise BrokerSettlementQueryError("non-admission endpoint returned NO_RECORD")
    _validate_exchange_status(
        response.status_code, body.outcome, allow_conflict=not allow_no_record
    )
    if (
        body.outcome not in (BrokerExchangeOutcome.NOT_ADMITTED, BrokerExchangeOutcome.NO_RECORD)
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
        header = response.headers.get("Livepeer-Non-Admission")
        if header is not None and header != body.non_admission:
            raise BrokerSettlementQueryError("non-admission header and body disagree")
    elif non_admission is not None:
        raise BrokerSettlementQueryError("non-admission evidence has the wrong outcome")

    values = body.model_dump(exclude={"settlement", "non_admission"})
    return BrokerExchangeResult(**values, settlement=settlement, non_admission=non_admission)


def _validate_exchange_status(
    status_code: int, outcome: BrokerExchangeOutcome, *, allow_conflict: bool = False
) -> None:
    expected = {
        BrokerExchangeOutcome.ACCOUNTING_PENDING: httpx.codes.ACCEPTED,
        BrokerExchangeOutcome.IN_FLIGHT: httpx.codes.ACCEPTED,
        BrokerExchangeOutcome.NO_RECORD: httpx.codes.NOT_FOUND,
        BrokerExchangeOutcome.ADMITTED_EVIDENCE_EXPIRED: (
            httpx.codes.CONFLICT if allow_conflict else httpx.codes.OK
        ),
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
