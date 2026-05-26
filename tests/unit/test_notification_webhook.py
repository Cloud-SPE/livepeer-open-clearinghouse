"""Tests for the outbound webhook channel — derive_secret, sign_body,
send_webhook retry/backoff."""

from __future__ import annotations

import base64
import hashlib
import hmac
import uuid

import httpx
import pytest

from livepeer_open_clearinghouse.domains.notifications.webhook import (
    derive_secret,
    send_webhook,
    sign_body,
)


def _make_mock_client(handler) -> httpx.AsyncClient:
    """Build an AsyncClient whose transport runs ``handler(request)``
    and returns the resulting Response. Avoids pulling respx into the
    server-side test deps."""
    transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(transport=transport, timeout=10.0)


@pytest.mark.unit
def test_derive_secret_is_deterministic() -> None:
    user_id = uuid.UUID("11111111-2222-3333-4444-555555555555")
    a = derive_secret("seed-1", user_id)
    b = derive_secret("seed-1", user_id)
    assert a == b
    assert a.startswith("whsec_")


@pytest.mark.unit
def test_derive_secret_differs_per_seed_and_user() -> None:
    user_a = uuid.UUID("11111111-2222-3333-4444-555555555555")
    user_b = uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    s1 = derive_secret("seed-1", user_a)
    s2 = derive_secret("seed-2", user_a)
    s3 = derive_secret("seed-1", user_b)
    assert s1 != s2
    assert s1 != s3


@pytest.mark.unit
def test_sign_body_matches_standard_webhooks() -> None:
    """Replicate the Standard-Webhooks signing recipe by hand and
    compare."""
    secret = derive_secret("seed-test", uuid.uuid4())
    raw_secret_b64 = secret.removeprefix("whsec_")
    key = base64.b64decode(raw_secret_b64 + "=" * (-len(raw_secret_b64) % 4))
    webhook_id, ts, body = "ev-1", "1700000000", b'{"x":1}'
    sig = sign_body(secret, webhook_id=webhook_id, timestamp=ts, body=body)
    expected_base = f"{webhook_id}.{ts}.".encode() + body
    expected = "v1," + base64.b64encode(
        hmac.new(key, expected_base, hashlib.sha256).digest()
    ).decode("ascii")
    assert sig == expected


async def test_send_webhook_2xx_returns_true() -> None:
    secret = derive_secret("seed-test", uuid.uuid4())
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        return httpx.Response(200)

    async with _make_mock_client(handler) as http:
        ok = await send_webhook(
            http=http,
            url="https://customer.test/hook",
            secret=secret,
            payload={"trigger": "test_ping"},
            max_retries=1,
        )
    assert ok is True
    assert len(calls) == 1


async def test_send_webhook_4xx_returns_false_without_retry() -> None:
    secret = derive_secret("seed-test", uuid.uuid4())
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        return httpx.Response(400)

    async with _make_mock_client(handler) as http:
        ok = await send_webhook(
            http=http,
            url="https://customer.test/hook",
            secret=secret,
            payload={"trigger": "test_ping"},
            max_retries=3,
        )
    assert ok is False
    assert len(calls) == 1  # no retry on 4xx


async def test_send_webhook_5xx_retries_then_gives_up() -> None:
    secret = derive_secret("seed-test", uuid.uuid4())
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        return httpx.Response(503)

    async with _make_mock_client(handler) as http:
        ok = await send_webhook(
            http=http,
            url="https://customer.test/hook",
            secret=secret,
            payload={"trigger": "test_ping"},
            max_retries=3,
        )
    assert ok is False
    assert len(calls) == 3


async def test_send_webhook_signs_headers() -> None:
    captured: dict = {}
    secret = derive_secret("seed-test", uuid.uuid4())

    def handler(req: httpx.Request) -> httpx.Response:
        captured["webhook-id"] = req.headers.get("webhook-id")
        captured["webhook-timestamp"] = req.headers.get("webhook-timestamp")
        captured["webhook-signature"] = req.headers.get("webhook-signature")
        captured["body"] = req.content
        return httpx.Response(200)

    async with _make_mock_client(handler) as http:
        ok = await send_webhook(
            http=http,
            url="https://customer.test/hook",
            secret=secret,
            payload={"trigger": "cap_reached"},
            max_retries=1,
        )
    assert ok is True
    assert captured["webhook-id"]
    assert captured["webhook-timestamp"]
    sig = captured["webhook-signature"]
    expected_sig = sign_body(
        secret,
        webhook_id=captured["webhook-id"],
        timestamp=captured["webhook-timestamp"],
        body=captured["body"],
    )
    assert sig == expected_sig
