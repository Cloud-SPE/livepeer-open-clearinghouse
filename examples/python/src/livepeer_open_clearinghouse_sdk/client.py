"""Async HTTP client for the Livepeer Open Clearinghouse gateway.

Handoff-mode SDK per exec-plan 002 (long-running sessions). Two
flows:

  - ``submit_job`` — cases (a)/(b)/(c). Atomic, post-settled, or
    streaming work. Composes ``POST /v1/jobs`` → broker call →
    ``POST /v1/jobs/{id}/settle`` so callers see a single function
    call returning the broker's response + final billing.

  - ``open_session`` — case (d). Long-running interaction. Returns
    a context-manager-like ``SessionHandle`` that exposes the
    broker URL + minted envelope; SDK consumer is responsible for
    the broker's WS / RTMP wire today. Full broker-side
    orchestration (refill loop, in-band Livepeer-Balance-Low,
    close) lands in the per-mode driver work tracked in the
    plan's "remaining Phase 2" items.

In handoff mode LOC is never in the broker data path — the SDK
talks to the broker directly using the minted ``payment_envelope``
as the ``Livepeer-Payment`` header.
"""

from __future__ import annotations

import asyncio
import base64
import os
import platform
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, cast

import httpx

from livepeer_open_clearinghouse_sdk import telemetry as telemetry_module
from livepeer_open_clearinghouse_sdk._generated import (
    CapabilityView as Capability,
)
from livepeer_open_clearinghouse_sdk._generated import (
    OfferingView as Offering,  # noqa: F401 — re-exported in __init__.py
)
from livepeer_open_clearinghouse_sdk._generated import (
    OrchestratorView as Orchestrator,
)
from livepeer_open_clearinghouse_sdk._generated import (
    RouteView,  # noqa: F401 — re-exported in __init__.py
)
from livepeer_open_clearinghouse_sdk.errors import OpenClearinghouseError, from_response


def _http2_available() -> bool:
    try:
        import h2  # noqa: F401
    except ImportError:
        return False
    return True

__all_generated__ = ("Capability", "Offering", "Orchestrator", "RouteView")

_DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=10.0, pool=5.0)

# SDK identity for the Livepeer-Open-Clearinghouse-SDK header.
# Operators key per-API-key trust scoring off this string.
SDK_LANG = "python"
SDK_VERSION = "0.2.0"
SDK_GIT_SHA = "dev"  # overwritten at packaging time
SDK_IDENTITY = f"{SDK_LANG}/{SDK_VERSION}/{SDK_GIT_SHA}"


@dataclass(frozen=True, slots=True)
class CapStatus:
    """Cap headroom snapshot returned with refill/settle responses."""

    session_pct_used: float
    spend_period_pct_used: float | None
    user_balance_pct_used: float | None
    operator_pool_pct_used: float | None
    will_refuse_next_refill: bool
    winddown_reason: str | None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CapStatus:
        return cls(
            session_pct_used=float(d["session_pct_used"]),
            spend_period_pct_used=(
                None if d.get("spend_period_pct_used") is None
                else float(d["spend_period_pct_used"])
            ),
            user_balance_pct_used=(
                None if d.get("user_balance_pct_used") is None
                else float(d["user_balance_pct_used"])
            ),
            operator_pool_pct_used=(
                None if d.get("operator_pool_pct_used") is None
                else float(d["operator_pool_pct_used"])
            ),
            will_refuse_next_refill=bool(d["will_refuse_next_refill"]),
            winddown_reason=d.get("winddown_reason"),
        )


@dataclass(frozen=True, slots=True)
class JobResult:
    """Final accounting for ``submit_job`` (a/b/c).

    Carries the broker's response body + status alongside the LOC-side
    settlement record. Customer code typically reads ``body`` for
    application output and may surface ``cap_status`` for UX.
    """

    body: Any
    status: int
    job_id: uuid.UUID
    work_id: str
    actual_units: int
    billed_value_wei: int
    refund_wei: int
    outcome: str
    cap_status: CapStatus
    request_id: str
    raw_headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SessionHandle:
    """Outbound from ``open_session`` (case d).

    Holds everything the consumer needs to drive the broker-side WS /
    RTMP wire themselves. The full automatic driver (refill loop,
    in-band balance-low handling, graceful close) is being built
    per-mode and will land as a richer ``SessionRunner`` wrapper in a
    later release.

    For now: ``payment_envelope`` is base64-encoded payment bytes;
    customer puts it in the ``Livepeer-Payment`` upgrade header.
    ``refill_endpoint`` / ``close_endpoint`` are LOC-relative paths
    the SDK uses to mint top-ups or finalize the session — call
    ``client.refill_session(session_id)`` / ``client.close_session(...)``
    helpers.
    """

    session_id: uuid.UUID
    work_id: str
    broker_url: str
    mode: str
    payment_envelope: str
    expected_value_wei: int
    funded_value_wei: int
    refill_endpoint: str
    close_endpoint: str


class OpenClearinghouseClient:
    """Async client for Livepeer Open Clearinghouse.

    Construct one per process and re-use. Internally it holds an httpx
    ``AsyncClient`` you should close via ``await client.aclose()`` —
    or use it as an async context manager.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout: httpx.Timeout = _DEFAULT_TIMEOUT,
        http: httpx.AsyncClient | None = None,
        sdk_identity: str = SDK_IDENTITY,
    ) -> None:
        if not api_key.startswith("pymth_"):
            raise ValueError("api_key looks wrong (expected to start with pymth_)")
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._http = http or httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout,
            headers={
                "X-API-Key": api_key,
                "Livepeer-Open-Clearinghouse-SDK": sdk_identity,
            },
            # HTTP/2 connection reuse for telemetry batches when the h2
            # extra is installed (`pip install httpx[http2]`). Falls
            # back to HTTP/1.1 keepalive otherwise — telemetry still
            # works, just opens a TCP connection per batch.
            http2=_http2_available(),
        )
        self._owns_http = http is None
        # Telemetry: mandatory per exec-plan 002 §"SDK telemetry (v1)".
        # No opt-out. Buffer + flush loop start lazily on first emit
        # (which is sdk.init below) so we don't need an event loop yet.
        self._telemetry = telemetry_module.TelemetryEmitter(http=self._http)
        self._telemetry_started = False
        self._sdk_identity = sdk_identity

    def _ensure_telemetry_started(self) -> None:
        """First call from inside a running event loop kicks off the
        background flush task and emits sdk.init."""
        if self._telemetry_started:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop yet — try again on the next operation
        self._telemetry.start()
        self._telemetry_started = True
        # sdk.init carries the universal fields of the v1 contract;
        # the operator-side ingest enrichment fills geo_region etc.
        self._telemetry.emit(
            event_type="sdk.init",
            payload={
                "lang": SDK_LANG,
                "semver": SDK_VERSION,
                "git_sha7": SDK_GIT_SHA,
                "runtime_version": f"python/{platform.python_version()}",
                "os": platform.system().lower(),
                "os_version": platform.release(),
                "process_id": os.getpid(),
            },
        )

    @property
    def telemetry(self) -> telemetry_module.TelemetryEmitter:
        """Direct access for advanced cases (e.g. customer-side emits
        from outside the SDK). Most users never touch this."""
        return self._telemetry

    async def __aenter__(self) -> OpenClearinghouseClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        # Drain the telemetry buffer before closing the HTTP client —
        # otherwise the final flush has no transport.
        await self._telemetry.aclose()
        if self._owns_http:
            await self._http.aclose()

    # ---- discovery ----

    async def list_capabilities(self) -> list[Capability]:
        r = await self._http.get("/v1/capabilities")
        return cast("list[Capability]", self._unwrap(r)["items"])

    async def list_orchestrators(
        self, *, capability: str | None = None
    ) -> list[Orchestrator]:
        params = {"capability": capability} if capability else None
        r = await self._http.get("/v1/orchestrators", params=params)
        return cast("list[Orchestrator]", self._unwrap(r)["items"])

    # ---- jobs (cases a/b/c) ----

    async def submit_job(
        self,
        *,
        capability: str,
        offering: str,
        estimated_units: int,
        body: dict[str, Any] | bytes,
        max_total_units: int | None = None,
        request_id: str | None = None,
        spec_version: str = "0.1",
        timeout: httpx.Timeout | float | None = None,
    ) -> JobResult:
        """One-shot mint → broker → settle for cases (a)/(b)/(c).

        Composes ``POST /v1/jobs`` (mint), the broker's ``POST /v1/cap``
        with the minted envelope, then ``POST /v1/jobs/{id}/settle``
        reading ``Livepeer-Work-Units`` from the broker's response.

        ``estimated_units`` is the SDK's best guess of what the call
        will consume; ``max_total_units`` is the worst-case ceiling LOC
        encumbers up front (defaults to ``estimated_units`` for case
        (a) where the SDK knows exactly; pass generous for case (b)).

        ``body`` is forwarded verbatim — dicts get JSON-serialized;
        raw bytes are sent as-is (use for multipart). **Don't put a
        ``model`` field in the body** for OpenAI-shaped requests; the
        orchestrator routes via the ``Livepeer-Offering`` header.

        Returns a :class:`JobResult` carrying the broker's response
        body + status alongside the LOC settlement (billed, refund,
        cap_status).

        Raises :class:`OpenClearinghouseError` on LOC-side errors
        (insufficient credit, no route, daemon unavailable, etc.).
        Broker-level non-2xx is returned in the result, not raised.
        """
        req_id = request_id or str(uuid.uuid4())
        timeout = timeout if timeout is not None else httpx.Timeout(60.0)

        self._ensure_telemetry_started()
        self._telemetry.emit(
            event_type="request.mint_started",
            correlation_id=req_id,
            payload={
                "capability": capability,
                "offering": offering,
                "estimated_units": estimated_units,
            },
        )
        mint_started_ns = time.monotonic_ns()

        # 1. Open the job
        try:
            open_resp = await self._http.post(
                "/v1/jobs",
                json={
                    "capability": capability,
                    "offering": offering,
                    "estimated_units": estimated_units,
                    "max_total_units": max_total_units,
                },
            )
            job = self._unwrap(open_resp)
        except Exception as exc:
            self._telemetry.emit(
                event_type="request.error",
                correlation_id=req_id,
                payload={
                    "phase": "mint",
                    "error_class": exc.__class__.__name__,
                    "error_code": getattr(exc, "code", None),
                },
            )
            raise
        mint_completed_ns = time.monotonic_ns()
        self._telemetry.emit(
            event_type="request.mint_completed",
            correlation_id=req_id,
            payload={
                "latency_ms": (mint_completed_ns - mint_started_ns) // 1_000_000,
                "loc_status_code": open_resp.status_code,
                "funded_value_wei": job.get("funded_value_wei"),
                "mode": job.get("mode"),
            },
        )
        job_id = uuid.UUID(job["job_id"])
        broker_url = job["broker_url"]
        envelope = job["payment_envelope"]
        mode = job["mode"]
        settle_endpoint = job["settle_endpoint"]

        # 2. Call the broker directly with the minted envelope
        headers: dict[str, str] = {
            "Livepeer-Capability": capability,
            "Livepeer-Offering": offering,
            "Livepeer-Payment": envelope,
            "Livepeer-Mode": mode,
            "Livepeer-Spec-Version": spec_version,
            "Livepeer-Request-Id": req_id,
        }
        async with httpx.AsyncClient(timeout=timeout) as broker:
            if isinstance(body, dict):
                headers.setdefault("Content-Type", "application/json")
                resp = await broker.post(
                    f"{broker_url.rstrip('/')}/v1/cap",
                    headers=headers,
                    json=body,
                )
            else:
                headers.setdefault("Content-Type", "application/octet-stream")
                resp = await broker.post(
                    f"{broker_url.rstrip('/')}/v1/cap",
                    headers=headers,
                    content=body,
                )

        # 3. Read actual_units from the broker's response. For
        # http-reqresp/http-multipart, this is the Livepeer-Work-Units
        # response header. For http-stream, it's an HTTP trailer —
        # httpx merges trailers into resp.headers for HTTP/1.1 chunked
        # responses once the body is fully consumed (.post() reads
        # the whole body before returning). On newer httpx versions
        # trailers ALSO show up under resp.trailing_headers; check
        # both to be safe.
        actual_units_str = resp.headers.get("livepeer-work-units")
        if not actual_units_str:
            trailing = getattr(resp, "trailing_headers", None)
            if trailing is not None:
                actual_units_str = trailing.get("livepeer-work-units")
        actual_units = int(actual_units_str) if actual_units_str else 0

        # Parse body for the caller
        ctype = resp.headers.get("content-type", "")
        if "json" in ctype:
            parsed: Any = resp.json()
        else:
            parsed = resp.text

        # 4. Settle. Best-effort — if this fails, the reconciliation
        # janitor on the LOC side will catch the unclosed session via
        # GetSessionDebits and finalize it.
        settlement_payload: dict[str, Any] = {"actual_units": actual_units}
        livepeer_settlement = resp.headers.get("livepeer-settlement")
        if livepeer_settlement:
            try:
                import json
                settlement_payload["settlement"] = json.loads(
                    base64.b64decode(livepeer_settlement)
                )
            except (ValueError, KeyError):
                pass  # malformed — let LOC's daemon reconciliation handle it
        self._telemetry.emit(
            event_type="request.settle_started",
            correlation_id=req_id,
        )
        settle_started_ns = time.monotonic_ns()
        try:
            settle_resp = await self._post_with_retry(
                settle_endpoint, json=settlement_payload
            )
            settled = self._unwrap(settle_resp)
        except Exception as exc:
            self._telemetry.emit(
                event_type="request.error",
                correlation_id=req_id,
                payload={
                    "phase": "settle",
                    "error_class": exc.__class__.__name__,
                    "error_code": getattr(exc, "code", None),
                },
            )
            raise
        self._telemetry.emit(
            event_type="request.settle_completed",
            correlation_id=req_id,
            payload={
                "latency_ms": (time.monotonic_ns() - settle_started_ns) // 1_000_000,
                "loc_status_code": settle_resp.status_code,
                "refund_wei": int(settled["refund_wei"]),
                "billed_value_wei": int(settled["billed_value_wei"]),
                "outcome": settled["outcome"],
            },
        )
        self._telemetry.emit(
            event_type="request.completed",
            correlation_id=req_id,
            payload={
                "capability": capability,
                "offering": offering,
                "mode": mode,
                "estimated_units": estimated_units,
                "actual_units": int(settled["actual_units"]),
                "billed_value_wei": int(settled["billed_value_wei"]),
                "refund_wei": int(settled["refund_wei"]),
                "outcome": settled["outcome"],
                "broker_url": broker_url,
            },
        )

        return JobResult(
            body=parsed,
            status=resp.status_code,
            job_id=job_id,
            work_id=job["work_id"],
            actual_units=int(settled["actual_units"]),
            billed_value_wei=int(settled["billed_value_wei"]),
            refund_wei=int(settled["refund_wei"]),
            outcome=settled["outcome"],
            cap_status=CapStatus.from_dict(settled["cap_status"]),
            request_id=req_id,
            raw_headers=dict(resp.headers),
        )

    # ---- sessions (case d) ----

    async def open_session(
        self,
        *,
        capability: str,
        offering: str,
        estimated_runway_units: int,
        max_total_units: int,
    ) -> SessionHandle:
        """Open a long-running session and return a SessionHandle.

        ``max_total_units`` is the same input across all case-(d)
        modes, but its operational guarantee differs by mode class:

        For **(d-bounded) modes** (``ws-realtime@v0``):
            Your session will spend AT MOST ``max_total_units``.
            It may end earlier; it will end no later than when this
            much is consumed. The session **cannot be extended**
            mid-flight — refills are not supported in these modes.

        For **(d-extensible) modes** (``session-control-plus-media@v0``,
        ``rtmp-ingress-hls-egress@v0``, ``live-session-remote-runner@v0``,
        ``live-session-gateway-ingest@v0``):
            Your session will spend AT MOST ``max_total_units``.
            Refills happen automatically within this ceiling. Refills
            stop and the session drains if a higher-tier cap
            (spend-period, operator-pool) is reached before
            ``max_total_units`` is exhausted.

        ``estimated_runway_units`` is the initial chunk LOC mints
        toward (a smaller fraction of ``max_total_units``). The
        SessionRunner refill loop tops up automatically as the broker
        signals balance-low.

        Returns a :class:`SessionHandle` carrying the broker URL +
        minted envelope. Use :class:`SessionRunner` for the automatic
        refill loop, or call :meth:`refill_session` /
        :meth:`close_session` directly for manual control.
        """
        self._ensure_telemetry_started()
        r = await self._http.post(
            "/v1/sessions",
            json={
                "capability": capability,
                "offering": offering,
                "estimated_runway_units": estimated_runway_units,
                "max_total_units": max_total_units,
            },
        )
        data = self._unwrap(r)
        handle = SessionHandle(
            session_id=uuid.UUID(data["session_id"]),
            work_id=data["work_id"],
            broker_url=data["broker_url"],
            mode=data["mode"],
            payment_envelope=data["payment_envelope"],
            expected_value_wei=int(data["expected_value_wei"]),
            funded_value_wei=int(data["funded_value_wei"]),
            refill_endpoint=data["refill_endpoint"],
            close_endpoint=data["close_endpoint"],
        )
        self._telemetry.emit(
            event_type="session.opened",
            correlation_id=handle.session_id,
            payload={
                "capability": capability,
                "offering": offering,
                "mode": handle.mode,
                "max_total_units": max_total_units,
                "initial_runway_units": estimated_runway_units,
            },
        )
        return handle

    async def refill_session(
        self,
        session_id: uuid.UUID | str,
        *,
        observed_consumed_units: int | None = None,
    ) -> dict[str, Any]:
        """Mint a top-up bound to an existing session. Returns the new
        payment_envelope + cap_status. SDK consumer is responsible for
        delivering the envelope to the broker via the mode-specific
        channel (``session.topup`` JSON frame for
        ``session-control-plus-media@v0``, HTTP POST to
        ``control.topup_url`` for the ``live-session-*`` modes).
        """
        self._telemetry.emit(
            event_type="session.refill_requested",
            correlation_id=str(session_id),
        )
        refill_started_ns = time.monotonic_ns()
        try:
            r = await self._http.post(
                f"/v1/sessions/{session_id}/refill",
                json={"observed_consumed_units": observed_consumed_units},
            )
            result = self._unwrap(r)
        except OpenClearinghouseError as exc:
            if exc.status == 402:
                # session.refill_denied is one of the critical events that
                # bypasses the batch timer — operators need to see it now.
                details = exc.details if isinstance(exc.details, dict) else {}
                self._telemetry.emit(
                    event_type="session.refill_denied",
                    correlation_id=str(session_id),
                    payload={
                        "which": details.get("which"),
                        "remaining_wei": details.get("remaining_wei"),
                    },
                )
            else:
                self._telemetry.emit(
                    event_type="session.error",
                    correlation_id=str(session_id),
                    payload={
                        "phase": "refill",
                        "error_class": exc.__class__.__name__,
                        "error_code": exc.code,
                    },
                )
            raise
        self._telemetry.emit(
            event_type="session.refill_granted",
            correlation_id=str(session_id),
            payload={
                "latency_ms": (time.monotonic_ns() - refill_started_ns) // 1_000_000,
                "refill_seq": result.get("refill_seq"),
                "funded_value_wei": result.get("funded_value_wei"),
                "cap_status": result.get("cap_status"),
            },
        )
        return result

    async def close_session(
        self,
        session_id: uuid.UUID | str,
        *,
        actual_units: int,
        outcome: str | None = None,
        settlement: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Explicitly close a session and finalize accounting."""
        body: dict[str, Any] = {"actual_units": actual_units}
        if outcome is not None:
            body["outcome"] = outcome
        if settlement is not None:
            body["settlement"] = settlement
        try:
            r = await self._http.post(f"/v1/sessions/{session_id}/close", json=body)
            result = self._unwrap(r)
        except Exception as exc:
            self._telemetry.emit(
                event_type="session.error",
                correlation_id=str(session_id),
                payload={
                    "phase": "close",
                    "error_class": exc.__class__.__name__,
                    "error_code": getattr(exc, "code", None),
                },
            )
            raise
        # session.closed is critical — flushes immediately.
        self._telemetry.emit(
            event_type="session.closed",
            correlation_id=str(session_id),
            payload={
                "actual_units": int(result.get("actual_units", 0)),
                "billed_value_wei": int(result.get("billed_value_wei", 0)),
                "refund_wei": int(result.get("refund_wei", 0)),
                "outcome": result.get("outcome"),
                "closed_by": "customer",
            },
        )
        return result

    async def get_session_status(
        self, session_id: uuid.UUID | str
    ) -> dict[str, Any]:
        """Read-only snapshot of a session's state + accounting."""
        r = await self._http.get(f"/v1/sessions/{session_id}")
        return self._unwrap(r)

    # ---- internals ----

    _SETTLE_MAX_RETRIES = 3

    async def _post_with_retry(
        self,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        max_retries: int = _SETTLE_MAX_RETRIES,
    ) -> httpx.Response:
        """POST that retries on transient failures (5xx, 429,
        connect/read errors). 4xx surfaces immediately — those won't
        change on retry. Exponential backoff 0.5s / 1s / 2s ...

        Used by the settle path so a transient LOC blip doesn't
        leave a session unsettled; the reconciliation janitor would
        catch it eventually, but a synchronous retry buys low
        latency for the common case.
        """
        backoff = 0.5
        last_resp: httpx.Response | None = None
        for attempt in range(1, max_retries + 1):
            try:
                resp = await self._http.post(path, json=json)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                if attempt >= max_retries:
                    raise
                logger = __import__("logging").getLogger(__name__)
                logger.info(
                    "openclearinghouse: settle retry %d/%d after transport error: %r",
                    attempt, max_retries, exc,
                )
                import asyncio  # noqa: PLC0415
                await asyncio.sleep(backoff)
                backoff *= 2.0
                continue
            last_resp = resp
            # 5xx / 429 → retry. 4xx → fail fast (we own the bug).
            if resp.status_code < 500 and resp.status_code != 429:
                return resp
            if attempt >= max_retries:
                return resp
            import asyncio  # noqa: PLC0415
            await asyncio.sleep(backoff)
            backoff *= 2.0
        assert last_resp is not None  # loop returned or raised
        return last_resp

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

    @property
    def http(self) -> httpx.AsyncClient:
        return self._http


def is_open_clearinghouse_error(exc: BaseException) -> bool:
    """Convenience for ``except`` blocks that don't want to import the
    typed errors. Returns True iff ``exc`` is an OpenClearinghouseError."""
    return isinstance(exc, OpenClearinghouseError)


# Decimal helper for any caller building their own balance math.
def wei_to_eth(amount_wei: int | Decimal) -> Decimal:
    """Convert wei → ETH, returning a Decimal."""
    return Decimal(amount_wei) / Decimal(10**18)
