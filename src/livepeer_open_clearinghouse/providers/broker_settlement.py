"""Read signed settlement envelopes from a Modules v2 broker."""

from __future__ import annotations

import base64
import binascii
import uuid
from typing import Any, Protocol
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class BrokerSettlementQueryError(Exception):
    """The broker lookup did not yield a trustworthy protocol response."""


class _SettlementSignature(BaseModel):
    model_config = ConfigDict(extra="forbid")

    algorithm: str = Field(min_length=1)
    canonicalization: str = Field(min_length=1)
    value: str = Field(min_length=1)


class _SettlementEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    payload: dict[str, Any]
    signature: _SettlementSignature


class BrokerSettlementClient(Protocol):
    """Boundary used by session reconciliation to query one logical session."""

    async def get_settlement(
        self, *, broker_url: str, gateway_session_id: uuid.UUID
    ) -> dict[str, Any] | None: ...


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
