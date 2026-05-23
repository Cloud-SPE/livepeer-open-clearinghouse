"""Async HTTP client for the PymtHouse gateway."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx

from pymthouse_sdk.errors import PymtHouseError, from_response

_DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=10.0, pool=5.0)


@dataclass(frozen=True, slots=True)
class Mint:
    """The shape returned by ``POST /v1/payments/mint``.

    ``payment_bytes`` goes verbatim into the ``Livepeer-Payment`` header
    on your request to the orchestrator. ``recipient_eth_address`` is
    the orch the route was selected for.
    """

    payment_id: uuid.UUID
    work_id: str
    payment_bytes: str
    expected_value_wei: Decimal
    funded_value_wei: Decimal
    recipient_eth_address: str

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Mint:
        return cls(
            payment_id=uuid.UUID(d["payment_id"]),
            work_id=d["work_id"],
            payment_bytes=d["payment_bytes"],
            expected_value_wei=Decimal(d["expected_value_wei"]),
            funded_value_wei=Decimal(d["funded_value_wei"]),
            recipient_eth_address=d["recipient_eth_address"],
        )


class PymtHouseClient:
    """Async client wrapping the handful of gateway endpoints you need.

    Construct one per process and re-use. Internally it holds an httpx
    ``AsyncClient`` that you should close via ``await client.aclose()``
    when you're done — or use it as an async context manager.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout: httpx.Timeout = _DEFAULT_TIMEOUT,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key.startswith("pymth_"):
            raise ValueError("api_key looks wrong (expected to start with pymth_)")
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._http = http or httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout,
            headers={"X-API-Key": api_key},
        )
        self._owns_http = http is None

    async def __aenter__(self) -> PymtHouseClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    # ---- discovery ----

    async def list_capabilities(self) -> list[dict[str, Any]]:
        r = await self._http.get("/v1/capabilities")
        return self._unwrap(r)["items"]

    async def list_orchestrators(
        self, *, capability: str | None = None
    ) -> list[dict[str, Any]]:
        params = {"capability": capability} if capability else None
        r = await self._http.get("/v1/orchestrators", params=params)
        return self._unwrap(r)["items"]

    # ---- payments ----

    async def mint_payment(
        self,
        *,
        capability: str,
        offering: str,
        work_units: int,
        idempotency_key: str | None = None,
    ) -> Mint:
        headers: dict[str, str] = {}
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        r = await self._http.post(
            "/v1/payments/mint",
            json={
                "capability": capability,
                "offering": offering,
                "work_units": work_units,
            },
            headers=headers,
        )
        return Mint.from_dict(self._unwrap(r))

    async def report_usage(
        self,
        *,
        payment_id: uuid.UUID | str,
        actual_work_units: int,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        headers: dict[str, str] = {}
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        r = await self._http.post(
            "/v1/usage/report",
            json={
                "payment_id": str(payment_id),
                "actual_work_units": actual_work_units,
            },
            headers=headers,
        )
        return self._unwrap(r)

    # ---- internals ----

    def _unwrap(self, response: httpx.Response) -> dict[str, Any]:
        if response.is_success:
            return response.json()
        try:
            body = response.json()
        except ValueError:
            body = {"detail": response.text}
        retry_after = response.headers.get("retry-after")
        raise from_response(
            status=response.status_code,
            body=body if isinstance(body, dict) else {"detail": str(body)},
            retry_after=int(retry_after) if retry_after and retry_after.isdigit() else None,
        )

    # ---- escape hatch ----

    @property
    def http(self) -> httpx.AsyncClient:
        """Use this if you need a one-off call to an endpoint we don't wrap yet."""
        return self._http


def is_pymthouse_error(exc: BaseException) -> bool:
    return isinstance(exc, PymtHouseError)
