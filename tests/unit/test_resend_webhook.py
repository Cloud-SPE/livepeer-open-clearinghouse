"""Unit tests for the Resend webhook signature verifier.

The verifier is pure (no DB) — body bytes + headers + secret + clock
in, success/exception out. We exercise the four checks the verifier
makes (missing headers, bad timestamp format, stale timestamp, bad
signature) and the happy path against a known-good signature we
compute the same way Resend would.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import UTC, datetime, timedelta

import pytest

from livepeer_open_clearinghouse.domains.notifications import service
from livepeer_open_clearinghouse.providers.clock import FrozenClock

SECRET = "whsec_dGVzdC1zZWNyZXQtZm9yLXVuaXQtdGVzdHMtMzJieQ=="  # base64("test-secret-for-unit-tests-32by")
DECODED_KEY = base64.b64decode(SECRET.removeprefix("whsec_"))


def _sign(*, webhook_id: str, ts: int, body: bytes) -> str:
    signed = f"{webhook_id}.{ts}.".encode() + body
    sig = base64.b64encode(hmac.new(DECODED_KEY, signed, hashlib.sha256).digest()).decode("ascii")
    return f"v1,{sig}"


@pytest.mark.unit
def test_verify_signature_happy_path() -> None:
    body = b'{"type":"email.delivered","data":{"email_id":"abc"}}'
    now = datetime(2026, 5, 23, 13, 0, 0, tzinfo=UTC)
    ts = int(now.timestamp())
    service.verify_signature(
        secret=SECRET,
        webhook_id="msg_1",
        webhook_timestamp=str(ts),
        webhook_signature=_sign(webhook_id="msg_1", ts=ts, body=body),
        body=body,
        clock=FrozenClock(now),
    )


@pytest.mark.unit
def test_verify_signature_rejects_missing_headers() -> None:
    now = datetime(2026, 5, 23, tzinfo=UTC)
    with pytest.raises(service.WebhookSignatureError, match="missing"):
        service.verify_signature(
            secret=SECRET,
            webhook_id=None,
            webhook_timestamp="0",
            webhook_signature="v1,xxx",
            body=b"{}",
            clock=FrozenClock(now),
        )


@pytest.mark.unit
def test_verify_signature_rejects_stale_timestamp() -> None:
    body = b"{}"
    now = datetime(2026, 5, 23, 13, 0, 0, tzinfo=UTC)
    # 10 minutes past — outside the 5-minute window.
    stale = int((now - timedelta(minutes=10)).timestamp())
    with pytest.raises(service.WebhookSignatureError, match="tolerance"):
        service.verify_signature(
            secret=SECRET,
            webhook_id="msg_1",
            webhook_timestamp=str(stale),
            webhook_signature=_sign(webhook_id="msg_1", ts=stale, body=body),
            body=body,
            clock=FrozenClock(now),
        )


@pytest.mark.unit
def test_verify_signature_rejects_non_numeric_timestamp() -> None:
    body = b"{}"
    with pytest.raises(service.WebhookSignatureError, match="non-integer"):
        service.verify_signature(
            secret=SECRET,
            webhook_id="msg_1",
            webhook_timestamp="not-a-number",
            webhook_signature="v1,xxx",
            body=body,
            clock=FrozenClock(datetime(2026, 5, 23, tzinfo=UTC)),
        )


@pytest.mark.unit
def test_verify_signature_rejects_bad_signature() -> None:
    body = b"{}"
    now = datetime(2026, 5, 23, 13, 0, 0, tzinfo=UTC)
    ts = int(now.timestamp())
    with pytest.raises(service.WebhookSignatureError, match="mismatch"):
        service.verify_signature(
            secret=SECRET,
            webhook_id="msg_1",
            webhook_timestamp=str(ts),
            webhook_signature="v1,deadbeef",
            body=body,
            clock=FrozenClock(now),
        )


@pytest.mark.unit
def test_verify_signature_accepts_one_of_multiple_signatures() -> None:
    """During key rotation senders emit multiple v1 signatures — any matching one wins."""
    body = b"{}"
    now = datetime(2026, 5, 23, 13, 0, 0, tzinfo=UTC)
    ts = int(now.timestamp())
    good = _sign(webhook_id="msg_1", ts=ts, body=body)
    combined = f"v1,deadbeef {good}"
    service.verify_signature(
        secret=SECRET,
        webhook_id="msg_1",
        webhook_timestamp=str(ts),
        webhook_signature=combined,
        body=body,
        clock=FrozenClock(now),
    )


@pytest.mark.unit
def test_status_for_event_maps_terminal_events() -> None:
    assert service._status_for_event("email.delivered") == "delivered"
    assert service._status_for_event("email.bounced") == "bounced"
    assert service._status_for_event("email.complained") == "complained"
    assert service._status_for_event("email.failed") == "failed"
    # Non-terminal events shouldn't overwrite the in-place status.
    assert service._status_for_event("email.opened") is None
    assert service._status_for_event("email.delivery_delayed") is None
