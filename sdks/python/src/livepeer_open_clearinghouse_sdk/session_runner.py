"""paid-session/v1 broker control driver with idempotent automatic refills."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any

import httpx

try:
    import websockets
    from websockets.asyncio.client import ClientConnection
    from websockets.exceptions import ConnectionClosed
except ImportError as _exc:  # pragma: no cover - declared dependency
    raise ImportError("SessionRunner requires the `websockets` package") from _exc

from livepeer_open_clearinghouse_sdk.client import OpenClearinghouseClient, SessionHandle
from livepeer_open_clearinghouse_sdk.errors import BrokerProtocolError, OpenClearinghouseError

_logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SessionBalance:
    status: str
    claimed_units: int
    debited_units: int
    unit: str
    runway_units: int
    runway_seconds_estimate: int | None
    will_refuse_next_refill: bool

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> SessionBalance:
        status = value.get("status")
        if status not in {"ok", "low", "exhausted"}:
            raise BrokerProtocolError(f"invalid session balance status {status!r}")
        try:
            return cls(
                status=status,
                claimed_units=int(value["claimed_units"]),
                debited_units=int(value["debited_units"]),
                unit=str(value["unit"]),
                runway_units=int(value["runway_units"]),
                runway_seconds_estimate=(
                    None
                    if value.get("runway_seconds_estimate") is None
                    else int(value["runway_seconds_estimate"])
                ),
                will_refuse_next_refill=bool(value["will_refuse_next_refill"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise BrokerProtocolError("broker returned a malformed balance object") from exc


@dataclass(frozen=True, slots=True)
class BrokerControl:
    status_url: str
    topup_url: str
    end_url: str
    events_ws: str | None


@dataclass(frozen=True, slots=True)
class BrokerSession:
    session_id: str
    work_id: str
    state: str
    runtime_schema: str
    runtime_public: dict[str, Any]
    grants: tuple[dict[str, Any], ...]
    credential: str
    lease_expires_at: str
    balance: SessionBalance
    control: BrokerControl


@dataclass(frozen=True, slots=True)
class RefillEvent:
    refill_seq: int | None
    expected_value_wei: int | None
    funded_value_wei: int | None
    cap_status: dict[str, Any] | None
    error: OpenClearinghouseError | None = None


@dataclass(frozen=True, slots=True)
class WinddownEvent:
    reason: str
    projected_end_at: str | None


RefillCallback = Callable[[RefillEvent], Awaitable[None] | None]
WinddownCallback = Callable[[WinddownEvent], Awaitable[None] | None]


class SessionRunner:
    """Open and control one paid-session/v1 broker session.

    HTTP is authoritative for open, status, top-up, and end. The optional
    control WebSocket only pushes balance and state changes early.
    """

    def __init__(
        self,
        *,
        client: OpenClearinghouseClient,
        handle: SessionHandle,
        on_refill_succeeded: RefillCallback | None = None,
        on_refill_refused: RefillCallback | None = None,
        on_winddown_warning: WinddownCallback | None = None,
        auto_close_on_disconnect: bool = False,
    ) -> None:
        self._client = client
        self._handle = handle
        self._on_refill_succeeded = on_refill_succeeded
        self._on_refill_refused = on_refill_refused
        self._on_winddown_warning = on_winddown_warning
        self._auto_close_on_disconnect = auto_close_on_disconnect
        self._broker_session: BrokerSession | None = None
        self._ws: ClientConnection | None = None
        self._listener_task: asyncio.Task[None] | None = None
        self._closed_event = asyncio.Event()
        self._final_settle: dict[str, Any] | None = None
        self._pending_refill_key: str | None = None
        self._pending_refill: dict[str, Any] | None = None
        self._closing = False

    @property
    def broker_session(self) -> BrokerSession | None:
        return self._broker_session

    @property
    def outcome(self) -> str | None:
        return None if self._final_settle is None else self._final_settle.get("outcome")

    @property
    def billed_value_wei(self) -> int | None:
        return None if self._final_settle is None else self._final_settle.get("billed_value_wei")

    @property
    def refund_wei(self) -> int | None:
        return None if self._final_settle is None else self._final_settle.get("refund_wei")

    async def __aenter__(self) -> SessionRunner:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        if not self._closed_event.is_set():
            await self.close(actual_units=0)

    async def start(self) -> BrokerSession:
        """Idempotently open the broker session and attach optional events."""
        if self._broker_session is not None:
            return self._broker_session
        url = f"{self._handle.broker_url.rstrip('/')}/v1/session"
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as broker:
            response = await broker.post(
                url,
                headers={
                    "Livepeer-Protocol": self._handle.protocol,
                    "Livepeer-Capability": self._handle.capability,
                    "Livepeer-Offering": self._handle.offering,
                    "Livepeer-Request-Id": self._handle.request_id,
                    "Livepeer-Payment": self._handle.payment_envelope,
                },
                json={
                    "gateway_session_id": str(self._handle.session_id),
                    "session_params": self._handle.session_params,
                },
            )
        response.raise_for_status()
        session = self._parse_open(response.json())
        self._broker_session = session
        if session.control.events_ws:
            self._ws = await websockets.connect(
                session.control.events_ws,
                additional_headers={"Authorization": f"Bearer {session.credential}"},
            )
            self._listener_task = asyncio.create_task(self._listen_ws())
        return session

    def _parse_open(self, data: Any) -> BrokerSession:
        if not isinstance(data, dict):
            raise BrokerProtocolError("broker session-open response must be an object")
        try:
            runtime = data["runtime"]
            control = data["control"]
            lease = data["lease"]
            schema = runtime["schema"]
            if schema != self._handle.session.descriptor_schema:
                raise BrokerProtocolError(
                    f"broker returned descriptor {schema!r}; expected "
                    f"{self._handle.session.descriptor_schema!r}"
                )
            if data["work_id"] != self._handle.work_id:
                raise BrokerProtocolError("broker session work_id does not match the payment")
            grants = runtime.get("grants", [])
            if not isinstance(runtime["public"], dict) or not isinstance(grants, list):
                raise TypeError
            parsed_control = BrokerControl(
                status_url=_required_string(control, "status_url"),
                topup_url=_required_string(control, "topup_url"),
                end_url=_required_string(control, "end_url"),
                events_ws=_optional_string(control, "events_ws"),
            )
            return BrokerSession(
                session_id=_required_string(data, "session_id"),
                work_id=_required_string(data, "work_id"),
                state=_required_string(data, "state"),
                runtime_schema=str(schema),
                runtime_public=dict(runtime["public"]),
                grants=tuple(dict(grant) for grant in grants),
                credential=_required_string(data, "credential"),
                lease_expires_at=_required_string(lease, "expires_at"),
                balance=SessionBalance.from_dict(data["balance"]),
                control=parsed_control,
            )
        except BrokerProtocolError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise BrokerProtocolError("broker returned a malformed session-open response") from exc

    async def status(self) -> dict[str, Any]:
        session = await self.start()
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as broker:
            response = await broker.get(
                session.control.status_url,
                headers={"Authorization": f"Bearer {session.credential}"},
            )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise BrokerProtocolError("broker session status must be an object")
        return data

    async def _listen_ws(self) -> None:
        assert self._ws is not None
        try:
            async for message in self._ws:
                await self._handle_ws_message(message)
        except ConnectionClosed:
            _logger.info("session_runner.events_ws_closed")
        finally:
            if self._auto_close_on_disconnect and not self._closing:
                with contextlib.suppress(Exception):
                    await self.close(actual_units=0)

    async def _handle_ws_message(self, message: str | bytes) -> None:
        if isinstance(message, bytes):
            return
        try:
            payload = json.loads(message)
        except json.JSONDecodeError:
            return
        if not isinstance(payload, dict):
            return
        if payload.get("type") == "session.balance":
            balance = payload.get("balance")
            if isinstance(balance, dict):
                await self.on_balance(SessionBalance.from_dict(balance))

    async def on_balance(self, balance: SessionBalance | dict[str, Any]) -> None:
        """Act on a normative broker balance snapshot."""
        parsed = (
            balance if isinstance(balance, SessionBalance) else SessionBalance.from_dict(balance)
        )
        if parsed.will_refuse_next_refill:
            await self._fire_winddown(WinddownEvent("broker_will_refuse_next_refill", None))
            return
        if parsed.status != "low":
            return
        if self._handle.session.refill == "bounded":
            await self._fire_winddown(WinddownEvent("bounded_runway_exhausting", None))
            return
        await self._refill(parsed.claimed_units)

    async def _refill(self, observed_units: int) -> None:
        session = await self.start()
        if self._pending_refill_key is None:
            self._pending_refill_key = str(uuid.uuid4())
        if self._pending_refill is None:
            try:
                self._pending_refill = await self._client.refill_session(
                    self._handle.session_id,
                    observed_consumed_units=observed_units,
                    request_id=self._pending_refill_key,
                )
            except OpenClearinghouseError as exc:
                await self._fire_refill_refused(RefillEvent(None, None, None, None, error=exc))
                return

        refill = self._pending_refill
        response = await self._post_topup(session, refill)
        if _broker_error(response) == "recipient_rotated":
            if refill.get("rebind_from") is not None:
                await self._end_unrecoverable_rotation()
                return
            predecessor = str(refill["work_id"])
            replacement_key = str(uuid.uuid4())
            self._pending_refill_key = replacement_key
            try:
                refill = await self._client.refill_session(
                    self._handle.session_id,
                    observed_consumed_units=observed_units,
                    request_id=replacement_key,
                    rebind_from=predecessor,
                    replaces_request_id=str(refill["request_id"]),
                )
            except OpenClearinghouseError as exc:
                await self._fire_refill_refused(RefillEvent(None, None, None, None, error=exc))
                return
            self._pending_refill = refill
            response = await self._post_topup(session, refill)
        if _broker_error(response) == "recipient_rotated":
            await self._end_unrecoverable_rotation()
            return
        if _broker_error(response) == "rebind_refused":
            await self._end_unrecoverable_rotation()
            return
        response.raise_for_status()
        broker_result = response.json()
        if refill.get("rebind_from") is not None:
            self._broker_session = replace(session, work_id=str(refill["work_id"]))
        if isinstance(broker_result, dict) and isinstance(broker_result.get("balance"), dict):
            broker_balance = SessionBalance.from_dict(broker_result["balance"])
            if broker_balance.will_refuse_next_refill:
                await self._fire_winddown(WinddownEvent("broker_will_refuse_next_refill", None))
        await self._fire_refill_succeeded(
            RefillEvent(
                refill_seq=refill.get("refill_seq"),
                expected_value_wei=refill.get("expected_value_wei"),
                funded_value_wei=refill.get("funded_value_wei"),
                cap_status=refill.get("cap_status"),
            )
        )
        self._pending_refill_key = None
        self._pending_refill = None

    async def _post_topup(self, session: BrokerSession, refill: dict[str, Any]) -> httpx.Response:
        headers = {
            "Authorization": f"Bearer {session.credential}",
            "Livepeer-Payment": str(refill["payment_envelope"]),
            "Livepeer-Request-Id": str(refill["request_id"]),
        }
        rebind_from = refill.get("rebind_from")
        if rebind_from is not None:
            headers["Livepeer-Rebind-From"] = str(rebind_from)
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as broker:
            return await broker.post(session.control.topup_url, headers=headers, json={})

    async def _end_unrecoverable_rotation(self) -> None:
        self._pending_refill_key = None
        self._pending_refill = None
        await self._fire_winddown(WinddownEvent("payment_unrecoverable", None))

    async def close(
        self,
        *,
        actual_units: int,
        outcome: str | None = None,
    ) -> dict[str, Any]:
        if self._final_settle is not None:
            return self._final_settle
        self._closing = True
        session = await self.start()
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as broker:
            response = await broker.post(
                session.control.end_url,
                headers={"Authorization": f"Bearer {session.credential}"},
                json={"reason": "gateway_close"},
            )
        response.raise_for_status()
        encoded_settlement = response.headers.get("livepeer-settlement")
        if not encoded_settlement:
            raise BrokerProtocolError(
                "broker end response missing Livepeer-Settlement",
                code="broker_protocol_error",
                status=response.status_code,
                details={"missing_headers": ["Livepeer-Settlement"]},
            )
        try:
            settlement = json.loads(base64.b64decode(encoded_settlement, validate=True))
        except (ValueError, json.JSONDecodeError) as exc:
            raise BrokerProtocolError(
                "broker end response has malformed Livepeer-Settlement",
                code="broker_protocol_error",
                status=response.status_code,
            ) from exc
        if not isinstance(settlement, dict):
            raise BrokerProtocolError(
                "broker end response has malformed Livepeer-Settlement",
                code="broker_protocol_error",
                status=response.status_code,
            )
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
        if self._listener_task is not None and not self._listener_task.done():
            self._listener_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._listener_task
        self._final_settle = await self._client.close_session(
            self._handle.session_id,
            actual_units=actual_units,
            outcome=outcome,
            settlement=settlement,
        )
        self._closed_event.set()
        return self._final_settle

    async def wait_closed(self) -> None:
        await self._closed_event.wait()

    async def _fire_refill_succeeded(self, event: RefillEvent) -> None:
        if self._on_refill_succeeded is not None:
            await _maybe_await(self._on_refill_succeeded(event))

    async def _fire_refill_refused(self, event: RefillEvent) -> None:
        if self._on_refill_refused is not None:
            await _maybe_await(self._on_refill_refused(event))

    async def _fire_winddown(self, event: WinddownEvent) -> None:
        if self._on_winddown_warning is not None:
            await _maybe_await(self._on_winddown_warning(event))


def _required_string(value: dict[str, Any], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result:
        raise BrokerProtocolError(f"broker response missing {key}")
    return result


def _broker_error(response: httpx.Response) -> str | None:
    if response.status_code != 409:
        return None
    return response.headers.get("Livepeer-Error")


def _optional_string(value: dict[str, Any], key: str) -> str | None:
    result = value.get(key)
    if result is None:
        return None
    if not isinstance(result, str) or not result:
        raise BrokerProtocolError(f"broker response has invalid {key}")
    return result


async def _maybe_await(value: Any) -> None:
    if asyncio.iscoroutine(value):
        await value
