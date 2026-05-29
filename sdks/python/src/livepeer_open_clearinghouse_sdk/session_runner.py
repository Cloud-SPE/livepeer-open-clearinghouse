"""SessionRunner — automatic refill loop for case-(d-extensible) modes.

Wraps the broker-side wire (WS or HTTP-control) and the LOC-side
refill loop into a single async context manager. Customer code
opens a session via the SDK, hands the resulting SessionHandle to
SessionRunner, and gets:

  - Automatic subscription to `Livepeer-Balance-Low` from the broker
  - Automatic refill: SDK calls LOC's refill endpoint, gets the
    new envelope, delivers it to the broker via the mode-specific
    channel
  - Optional callbacks: ``on_refill_succeeded``, ``on_refill_refused``,
    ``on_winddown_warning``
  - Graceful close on cap-refusal or broker disconnect

Per exec-plan 002 § "Refill delivery wire shapes (per mode)":

  - ``session-control-plus-media@v0`` — WS to broker; ``session.topup``
    JSON frame for refill delivery; ``Livepeer-Balance-Low`` is a
    control-WS frame.
  - ``live-session-remote-runner@v0`` / ``live-session-gateway-ingest@v0`` —
    HTTP POST to ``control.topup_url`` (broker advertises it).
    Balance-low signaling is mode-specific; in practice the
    caller's media-plane disconnect is the canonical "session
    ended" signal here.
  - ``rtmp-ingress-hls-egress@v0`` — same WS pattern as
    session-control-plus-media when ``control_url`` is opened;
    otherwise balance-low manifests as an RTMP disconnect.
  - ``ws-realtime@v0`` — BOUNDED. SessionRunner refuses to refill;
    fires ``on_winddown_warning`` when balance-low arrives and lets
    the session drain naturally.

This is a reference implementation. Customers who need their own
broker wire (e.g. RTMP ingest from their own encoder) can skip
SessionRunner and call ``client.refill_session`` / ``client.close_session``
directly while running their own broker connection.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

try:
    import websockets
    from websockets.asyncio.client import ClientConnection
    from websockets.exceptions import ConnectionClosed
except ImportError as _exc:  # pragma: no cover — declared dep
    raise ImportError(
        "SessionRunner requires the `websockets` package. "
        "Install with `pip install livepeer-open-clearinghouse-sdk[ws]` "
        "or add `websockets` to your dependencies."
    ) from _exc

from livepeer_open_clearinghouse_sdk.client import (
    OpenClearinghouseClient,
    SessionHandle,
)
from livepeer_open_clearinghouse_sdk.errors import OpenClearinghouseError

_logger = logging.getLogger(__name__)

# Modes that have no protocol-level topup. SessionRunner refuses to
# refill these and fires the winddown callback instead.
BOUNDED_MODES: frozenset[str] = frozenset({"ws-realtime@v0"})

# Modes that deliver refill via a control-WS JSON frame.
WS_TOPUP_MODES: frozenset[str] = frozenset(
    {
        "session-control-plus-media@v0",
        "rtmp-ingress-hls-egress@v0",
    }
)

# Modes that deliver refill via HTTP POST to control.topup_url.
HTTP_TOPUP_MODES: frozenset[str] = frozenset(
    {
        "live-session-remote-runner@v0",
        "live-session-gateway-ingest@v0",
    }
)


@dataclass(frozen=True, slots=True)
class RefillEvent:
    """Payload to ``on_refill_succeeded`` / ``on_refill_refused``."""

    refill_seq: int | None
    expected_value_wei: int | None
    funded_value_wei: int | None
    cap_status: dict[str, Any] | None
    error: OpenClearinghouseError | None = None


@dataclass(frozen=True, slots=True)
class WinddownEvent:
    """Payload to ``on_winddown_warning``."""

    reason: str
    projected_end_at: str | None


# Convenient type aliases for the callbacks.
RefillCallback = Callable[[RefillEvent], Awaitable[None] | None]
WinddownCallback = Callable[[WinddownEvent], Awaitable[None] | None]


class SessionRunner:
    """Run a long-running session with automatic refills.

    Usage::

        handle = await client.open_session(...)
        async with SessionRunner(
            client=client,
            handle=handle,
            on_refill_succeeded=lambda e: print("refilled", e.refill_seq),
            on_refill_refused=lambda e: print("refused", e.error),
            on_winddown_warning=lambda w: print("ending:", w.reason),
        ) as runner:
            # ... do work over runner.broker_ws (or the mode-specific
            # channel) ...
            await runner.wait_closed()
            # Final accounting:
            print(runner.outcome, runner.billed_value_wei, runner.refund_wei)

    The runner exits the context manager once the session is closed
    (either by the customer via ``await runner.close()``, by the
    broker via disconnect, or by LOC refusing a refill).

    For ``ws-realtime@v0`` sessions: the runner connects, observes
    balance-low signals, fires ``on_winddown_warning``, and lets
    the broker close at balance-zero. No refills are attempted.
    """

    def __init__(
        self,
        *,
        client: OpenClearinghouseClient,
        handle: SessionHandle,
        on_refill_succeeded: RefillCallback | None = None,
        on_refill_refused: RefillCallback | None = None,
        on_winddown_warning: WinddownCallback | None = None,
        auto_close_on_disconnect: bool = True,
    ) -> None:
        self._client = client
        self._handle = handle
        self._on_refill_succeeded = on_refill_succeeded
        self._on_refill_refused = on_refill_refused
        self._on_winddown_warning = on_winddown_warning
        self._auto_close_on_disconnect = auto_close_on_disconnect

        self._ws: ClientConnection | None = None
        self._control_topup_url: str | None = None
        self._listener_task: asyncio.Task[None] | None = None
        self._closed_event = asyncio.Event()
        self._final_settle: dict[str, Any] | None = None

        # Mode classification
        self._is_bounded = handle.mode in BOUNDED_MODES
        self._uses_ws_topup = handle.mode in WS_TOPUP_MODES
        self._uses_http_topup = handle.mode in HTTP_TOPUP_MODES

    # ---- public read accessors (populated on close) ----

    @property
    def outcome(self) -> str | None:
        """Settlement outcome string; None until close completes."""
        return None if self._final_settle is None else self._final_settle.get("outcome")

    @property
    def billed_value_wei(self) -> int | None:
        return None if self._final_settle is None else self._final_settle.get("billed_value_wei")

    @property
    def refund_wei(self) -> int | None:
        return None if self._final_settle is None else self._final_settle.get("refund_wei")

    # ---- async context manager ----

    async def __aenter__(self) -> SessionRunner:
        await self._start()
        return self

    async def __aexit__(self, *_: object) -> None:
        if not self._closed_event.is_set():
            await self.close(actual_units=0)

    # ---- lifecycle ----

    async def _start(self) -> None:
        """Open the broker connection appropriate to the mode."""
        if self._uses_ws_topup or self._handle.mode in BOUNDED_MODES:
            # Both ws-realtime and the WS-topup modes connect WS to broker.
            self._ws = await websockets.connect(
                self._handle.broker_url,
                additional_headers={
                    "Livepeer-Payment": self._handle.payment_envelope,
                    "Livepeer-Mode": self._handle.mode,
                },
            )
            self._listener_task = asyncio.create_task(self._listen_ws())
        elif self._uses_http_topup:
            # live-session-*: open via POST /v1/cap to get the session
            # response which includes control.topup_url. The customer
            # owns the media-plane wire; SessionRunner only handles
            # refill delivery.
            await self._open_live_session()
        else:
            raise OpenClearinghouseError(
                f"SessionRunner: unsupported mode {self._handle.mode!r}",
                code="unsupported_mode",
                status=None,
            )

    async def _open_live_session(self) -> None:
        """For live-session-* modes: POST to broker /v1/cap to fetch
        the session-open response (which carries control.topup_url)."""
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as broker:
            resp = await broker.post(
                f"{self._handle.broker_url.rstrip('/')}/v1/cap",
                headers={
                    "Livepeer-Payment": self._handle.payment_envelope,
                    "Livepeer-Mode": self._handle.mode,
                    "Content-Type": "application/json",
                },
                json={},
            )
        resp.raise_for_status()
        data = resp.json()
        control = data.get("control", {})
        self._control_topup_url = control.get("topup_url")
        if not self._control_topup_url:
            raise OpenClearinghouseError(
                "broker session-open response missing control.topup_url",
                code="protocol_error",
                status=None,
            )

    async def _listen_ws(self) -> None:
        """Read broker WS frames; dispatch balance-low signals."""
        assert self._ws is not None
        try:
            async for message in self._ws:
                await self._handle_ws_message(message)
        except ConnectionClosed:
            _logger.info(
                "session_runner.ws_closed", extra={"session_id": str(self._handle.session_id)}
            )
        finally:
            if self._auto_close_on_disconnect and not self._closed_event.is_set():
                # Broker dropped the WS; finalize on our side.
                with contextlib.suppress(Exception):
                    await self.close(actual_units=0)

    async def _handle_ws_message(self, message: str | bytes) -> None:
        if isinstance(message, bytes):
            # Binary frames are capability payload — not our concern.
            return
        try:
            payload = json.loads(message)
        except json.JSONDecodeError:
            return
        if not isinstance(payload, dict):
            return

        msg_type = payload.get("type")
        if msg_type == "session.balance.low" or msg_type == "Livepeer-Balance-Low":
            await self._on_balance_low(payload)
        # session.balance.refilled is an ack from the backend that we
        # don't need to act on directly; surface via the refill cb.
        # Other capability frames pass through unhandled.

    async def _on_balance_low(self, payload: dict[str, Any]) -> None:
        if self._is_bounded:
            # ws-realtime can't refill — fire winddown and let the
            # broker close at balance-zero.
            await self._fire_winddown(
                WinddownEvent(
                    reason="ws_session_exhausting",
                    projected_end_at=payload.get("projected_end_at"),
                )
            )
            return

        # Extensible mode: request a refill from LOC and deliver to broker.
        try:
            refill = await self._client.refill_session(
                self._handle.session_id,
                observed_consumed_units=payload.get("observed_consumed_units"),
            )
        except OpenClearinghouseError as exc:
            event = RefillEvent(
                refill_seq=None,
                expected_value_wei=None,
                funded_value_wei=None,
                cap_status=None,
                error=exc,
            )
            await self._fire_refill_refused(event)
            # Don't kill the WS immediately — let the broker drain
            # naturally so the customer can observe the close cleanly.
            return

        # Deliver to broker via mode-specific channel
        envelope = refill["payment_envelope"]
        cap_status = refill.get("cap_status", {})
        if self._uses_ws_topup:
            await self._deliver_topup_ws(envelope)
        elif self._uses_http_topup:
            await self._deliver_topup_http(envelope)

        await self._fire_refill_succeeded(
            RefillEvent(
                refill_seq=refill.get("refill_seq"),
                expected_value_wei=refill.get("expected_value_wei"),
                funded_value_wei=refill.get("funded_value_wei"),
                cap_status=cap_status,
            )
        )

        # Check the winddown signal
        if cap_status and cap_status.get("will_refuse_next_refill"):
            await self._fire_winddown(
                WinddownEvent(
                    reason=cap_status.get("winddown_reason", "cap_imminent"),
                    projected_end_at=None,
                )
            )

    async def _deliver_topup_ws(self, envelope: str) -> None:
        """Send the session.topup JSON frame on the control WS."""
        assert self._ws is not None
        frame = {
            "type": "session.topup",
            "body": {"payment_header": envelope},
        }
        await self._ws.send(json.dumps(frame))

    async def _deliver_topup_http(self, envelope: str) -> None:
        """POST the refill envelope to the broker's control.topup_url."""
        assert self._control_topup_url is not None
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as broker:
            resp = await broker.post(
                self._control_topup_url,
                headers={"Livepeer-Payment": envelope, "Content-Type": "application/json"},
                json={"gateway_session_id": str(self._handle.session_id)},
            )
        resp.raise_for_status()

    # ---- callbacks ----

    async def _fire_refill_succeeded(self, event: RefillEvent) -> None:
        if self._on_refill_succeeded is None:
            return
        await _maybe_await(self._on_refill_succeeded(event))

    async def _fire_refill_refused(self, event: RefillEvent) -> None:
        if self._on_refill_refused is None:
            return
        await _maybe_await(self._on_refill_refused(event))

    async def _fire_winddown(self, event: WinddownEvent) -> None:
        if self._on_winddown_warning is None:
            return
        await _maybe_await(self._on_winddown_warning(event))

    # ---- close ----

    async def close(
        self,
        *,
        actual_units: int,
        outcome: str | None = None,
        settlement: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Close the session and finalize accounting on LOC.

        Idempotent in spirit: a second call returns the cached final
        settle dict from the first one.
        """
        if self._final_settle is not None:
            return self._final_settle

        # Tear down the broker WS (if any) first so the broker knows
        # we're done.
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
        if self._listener_task is not None and not self._listener_task.done():
            self._listener_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._listener_task

        # Tell LOC the session is closed.
        result = await self._client.close_session(
            self._handle.session_id,
            actual_units=actual_units,
            outcome=outcome,
            settlement=settlement,
        )
        self._final_settle = result
        self._closed_event.set()
        return result

    async def wait_closed(self) -> None:
        """Block until the session is closed (by any path)."""
        await self._closed_event.wait()


async def _maybe_await(value: Any) -> None:
    """Allow callbacks to be either sync or async."""
    if asyncio.iscoroutine(value):
        await value
