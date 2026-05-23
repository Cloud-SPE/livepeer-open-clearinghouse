"""Async HTTP client for the Livepeer Open Clearinghouse gateway."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx

from livepeer_open_clearinghouse_sdk.errors import OpenClearinghouseError, from_response

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


@dataclass(frozen=True, slots=True)
class JobResult:
    """Return shape of :py:meth:`OpenClearinghouseClient.submit_job`.

    The orchestrator's response body is forwarded verbatim under
    ``body`` (parsed JSON when the response is JSON, raw text otherwise).
    Side-channel info from the gateway+payment-daemon round-trip is
    surfaced in dedicated fields so callers don't need to introspect
    headers.
    """

    body: Any
    status: int
    payment_id: uuid.UUID
    recipient_eth_address: str
    request_id: str
    raw_headers: dict[str, str]


class OpenClearinghouseClient:
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

    async def __aenter__(self) -> OpenClearinghouseClient:
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

    async def list_orchestrators(self, *, capability: str | None = None) -> list[dict[str, Any]]:
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

    async def submit_job(
        self,
        *,
        capability: str,
        offering: str,
        work_units: int,
        body: dict[str, Any] | bytes,
        idempotency_key: str | None = None,
        request_id: str | None = None,
        mode: str = "http-reqresp@v0",
        spec_version: str = "0.1",
        timeout: httpx.Timeout | float | None = None,
    ) -> JobResult:
        """Mint a payment, route to an orchestrator, return its response.

        The load-bearing convenience method: handles route selection +
        payment mint + the orch HTTP call with the canonical
        ``POST <broker>/v1/cap`` shape and the five Livepeer headers.

        ``body`` is forwarded verbatim — dicts get JSON-serialised, raw
        bytes are sent as-is (use for multipart). **Don't put a ``model``
        field in the body** for OpenAI-shaped requests; the orchestrator
        routes via the ``Livepeer-Offering`` header and most upstreams
        (vLLM, etc.) will 404 on a mismatched model name. The orch picks
        the model bound to the offering.

        ``request_id`` defaults to a fresh UUID; pass an explicit value
        if your app already has a per-request ID it wants to thread.

        Raises :class:`OpenClearinghouseError` if the gateway rejects
        the mint. Orchestrator-level errors are returned as a
        :class:`JobResult` with the non-success status code — caller
        decides whether to raise.
        """
        # 1. Route selection — first orch advertising this offering wins.
        route = await self._http.get(
            "/v1/routes",
            params={"capability": capability, "offering": offering},
        )
        route_view = self._unwrap(route)
        broker_url: str = route_view["worker_url"]

        # 2. Mint a payment for this orch.
        mint = await self.mint_payment(
            capability=capability,
            offering=offering,
            work_units=work_units,
            idempotency_key=idempotency_key,
        )

        # 3. Build the request to the orch.
        req_id = request_id or str(uuid.uuid4())
        headers: dict[str, str] = {
            "Livepeer-Capability": capability,
            "Livepeer-Offering": offering,
            "Livepeer-Payment": mint.payment_bytes,
            "Livepeer-Mode": mode,
            "Livepeer-Spec-Version": spec_version,
            "Livepeer-Request-Id": req_id,
        }
        # Detached HTTP client — the orch is a third-party URL, not our
        # base_url. We open a one-shot AsyncClient so the timeout +
        # connection state are scoped to this call.
        timeout = timeout if timeout is not None else httpx.Timeout(60.0)
        async with httpx.AsyncClient(timeout=timeout) as orch:
            if isinstance(body, dict):
                headers.setdefault("Content-Type", "application/json")
                resp = await orch.post(
                    f"{broker_url.rstrip('/')}/v1/cap",
                    headers=headers,
                    json=body,
                )
            else:
                headers.setdefault(
                    "Content-Type", "application/octet-stream"
                )
                resp = await orch.post(
                    f"{broker_url.rstrip('/')}/v1/cap",
                    headers=headers,
                    content=body,
                )

        # 4. Parse + wrap. JSON when possible; raw text otherwise.
        ctype = resp.headers.get("content-type", "")
        if "json" in ctype:
            parsed: Any = resp.json()
        else:
            parsed = resp.text
        return JobResult(
            body=parsed,
            status=resp.status_code,
            payment_id=mint.payment_id,
            recipient_eth_address=mint.recipient_eth_address,
            request_id=req_id,
            raw_headers=dict(resp.headers),
        )

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


def is_open_clearinghouse_error(exc: BaseException) -> bool:
    return isinstance(exc, OpenClearinghouseError)
