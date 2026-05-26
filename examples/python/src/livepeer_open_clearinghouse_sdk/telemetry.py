"""SDK-side telemetry emitter.

Implements the v1 contract from exec-plan 002 §"SDK telemetry":

  - Fire-and-forget: telemetry MUST NOT block the data plane.
  - Batched: flush at 100 events or 5s, whichever comes first.
  - Flush-on-critical: ``*.error``, ``session.refill_denied``,
    ``session.closed`` bypass the batch timer.
  - Buffer cap: 10K events; oldest dropped on overflow + WARN log.
  - 3 retries with exponential backoff on 5xx / network failure;
    drop after that.
  - gzip request body when payload > 1 KiB.
  - HTTP/2 connection reuse to LOC — when the caller passes a
    pre-built ``httpx.AsyncClient`` with ``http2=True``, the emitter
    inherits it.

The emitter is mandatory; there is no ``telemetry=False`` flag.
This is the operator-side contract — customers who want to opt out
of operational instrumentation use operator-side ingest filtering,
not an in-SDK switch.
"""

from __future__ import annotations

import asyncio
import contextlib
import gzip
import json
import logging
import uuid
from collections import deque
from datetime import UTC, datetime
from typing import Any

import httpx

logger = logging.getLogger(__name__)


# Defaults match exec-plan 002 §"Mechanism".
DEFAULT_BATCH_SIZE: int = 100
DEFAULT_FLUSH_SECONDS: float = 5.0
DEFAULT_BUFFER_CAP: int = 10_000
DEFAULT_RETRIES: int = 3
DEFAULT_GZIP_THRESHOLD_BYTES: int = 1024

# Events that bypass the batch timer.
CRITICAL_EVENT_TYPES: frozenset[str] = frozenset(
    {
        # *.error matched via prefix below
        "session.refill_denied",
        "session.closed",
    }
)


def _is_critical(event_type: str) -> bool:
    return event_type in CRITICAL_EVENT_TYPES or event_type.endswith(".error")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class TelemetryEmitter:
    """In-process telemetry buffer + flusher.

    Owned by :class:`OpenClearinghouseClient`. Construct via
    :meth:`OpenClearinghouseClient.__init__`; do not instantiate
    directly. ``aclose()`` drains the remaining buffer with a final
    best-effort flush.
    """

    def __init__(
        self,
        *,
        http: httpx.AsyncClient,
        endpoint: str = "/v1/telemetry",
        batch_size: int = DEFAULT_BATCH_SIZE,
        flush_interval_seconds: float = DEFAULT_FLUSH_SECONDS,
        buffer_cap: int = DEFAULT_BUFFER_CAP,
        max_retries: int = DEFAULT_RETRIES,
        gzip_threshold_bytes: int = DEFAULT_GZIP_THRESHOLD_BYTES,
    ) -> None:
        self._http = http
        self._endpoint = endpoint
        self._batch_size = batch_size
        self._flush_interval = flush_interval_seconds
        self._buffer_cap = buffer_cap
        self._max_retries = max_retries
        self._gzip_threshold = gzip_threshold_bytes

        self._buffer: deque[dict[str, Any]] = deque(maxlen=buffer_cap)
        self._dropped_count: int = 0
        self._flush_event: asyncio.Event = asyncio.Event()
        self._closed: bool = False
        self._flush_task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Begin the background flush loop. Idempotent."""
        if self._flush_task is None and not self._closed:
            self._flush_task = asyncio.create_task(self._flush_loop())

    def emit(
        self,
        *,
        event_type: str,
        event_schema_version: int = 1,
        correlation_id: uuid.UUID | str | None = None,
        payload: dict[str, Any] | None = None,
        client_ts: str | None = None,
    ) -> None:
        """Append one event to the buffer. Never raises.

        Triggers an immediate flush when:
          - the buffer crosses the batch-size threshold, or
          - the event is critical (``*.error``,
            ``session.refill_denied``, ``session.closed``).
        """
        if self._closed:
            return
        event = {
            "event_type": event_type,
            "event_schema_version": event_schema_version,
            "correlation_id": str(correlation_id) if correlation_id is not None else None,
            "client_ts": client_ts or _now_iso(),
            "payload": payload or {},
        }
        if len(self._buffer) == self._buffer_cap:
            self._dropped_count += 1
            logger.warning(
                "telemetry buffer full; dropped oldest event (total dropped=%d)",
                self._dropped_count,
            )
        self._buffer.append(event)

        if _is_critical(event_type) or len(self._buffer) >= self._batch_size:
            self._flush_event.set()

    async def aclose(self) -> None:
        """Stop the flush loop after one last drain. Idempotent."""
        if self._closed:
            return
        self._closed = True
        self._flush_event.set()
        if self._flush_task is not None:
            try:
                await asyncio.wait_for(self._flush_task, timeout=10.0)
            except (TimeoutError, asyncio.CancelledError):
                self._flush_task.cancel()
            self._flush_task = None
        # Final drain — best-effort, swallow any failure.
        if self._buffer:
            await self._flush_once()

    @property
    def dropped_count(self) -> int:
        """How many events were silently dropped due to buffer overflow."""
        return self._dropped_count

    @property
    def buffer_size(self) -> int:
        """Current count of unflushed events."""
        return len(self._buffer)

    # --- internals ----------------------------------------------------------

    async def _flush_loop(self) -> None:
        """Wait for either the flush event or the periodic deadline."""
        while not self._closed:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._flush_event.wait(), timeout=self._flush_interval
                )
            self._flush_event.clear()
            if self._buffer:
                await self._flush_once()

    async def _flush_once(self) -> None:
        """Drain the buffer in one batch attempt. Best-effort; never
        raises into the data plane."""
        if not self._buffer:
            return
        batch = list(self._buffer)
        self._buffer.clear()
        body = json.dumps({"events": batch}).encode("utf-8")
        headers: dict[str, str] = {"content-type": "application/json"}
        if len(body) > self._gzip_threshold:
            body = gzip.compress(body)
            headers["content-encoding"] = "gzip"
        await self._send_with_retry(body=body, headers=headers, event_count=len(batch))

    async def _send_with_retry(
        self, *, body: bytes, headers: dict[str, str], event_count: int
    ) -> None:
        backoff = 0.5
        for attempt in range(1, self._max_retries + 1):
            try:
                resp = await self._http.post(
                    self._endpoint, content=body, headers=headers
                )
                if resp.status_code < 500 and resp.status_code != 429:
                    return  # success or client error (don't retry)
                logger.debug(
                    "telemetry flush returned %s (attempt %d/%d)",
                    resp.status_code,
                    attempt,
                    self._max_retries,
                )
            except Exception as exc:
                logger.debug(
                    "telemetry flush error: %r (attempt %d/%d)",
                    exc,
                    attempt,
                    self._max_retries,
                )
            if attempt < self._max_retries:
                await asyncio.sleep(backoff)
                backoff *= 2.0
        logger.warning("telemetry flush dropped %d events after retries", event_count)
