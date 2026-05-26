"""Outbound webhook sender — Standard-Webhooks v1 signing on POST.

Per-user signing secret is derived deterministically from a server
seed so we don't need to store ciphertext:

    secret = HMAC_SHA256(WEBHOOK_SIGNING_SEED, user_id_bytes)

The customer sees the secret exactly once at config-creation time
and stores it locally. LOC re-derives on every send and signs the
body the Standard-Webhooks way:

    base = f"{webhook_id}.{timestamp}.{body}"
    signature = "v1," + base64(HMAC_SHA256(secret, base))

Outbound headers:

    webhook-id          UUID v4 per delivery
    webhook-timestamp   unix seconds
    webhook-signature   space-separated list, "v1,<sig>" entries

Verification on the customer side mirrors the existing inbound
Resend webhook code in :mod:`livepeer_open_clearinghouse.domains.notifications.service`.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
import uuid
from typing import Any

import httpx

from livepeer_open_clearinghouse.providers.telemetry import get_logger

logger = get_logger(__name__)

_HTTP_2XX_MIN = 200
_HTTP_3XX_MIN = 300
_HTTP_5XX_MIN = 500
_HTTP_TOO_MANY = 429


def derive_secret(seed: str, user_id: uuid.UUID) -> str:
    """Derive the per-user Standard-Webhooks secret.

    Returns a ``whsec_`` + base64 string the customer plugs into
    their verifier (matches the Resend / Svix convention).
    """
    mac = hmac.new(seed.encode("utf-8"), user_id.bytes, hashlib.sha256).digest()
    return "whsec_" + base64.b64encode(mac).decode("ascii").rstrip("=")


def sign_body(secret: str, *, webhook_id: str, timestamp: str, body: bytes) -> str:
    """Compute the Standard-Webhooks signature string for one POST."""
    raw_secret = secret.removeprefix("whsec_")
    try:
        key = base64.b64decode(raw_secret + "=" * (-len(raw_secret) % 4))
    except (ValueError, base64.binascii.Error):  # type: ignore[attr-defined]
        key = secret.encode("utf-8")
    base = f"{webhook_id}.{timestamp}.".encode() + body
    digest = hmac.new(key, base, hashlib.sha256).digest()
    return "v1," + base64.b64encode(digest).decode("ascii")


async def send_webhook(
    *,
    http: httpx.AsyncClient | None,
    url: str,
    secret: str,
    payload: dict[str, Any],
    max_retries: int = 3,
    timeout_seconds: float = 10.0,
) -> bool:
    """POST the payload to ``url`` with Standard-Webhooks signing.

    Returns True on a 2xx response within ``max_retries`` attempts;
    False on any persistent failure. Exponential backoff between
    retries (500 ms / 1 s / 2 s ...).

    ``http`` may be a shared AsyncClient; when ``None``, opens a new
    one and closes it at the end.
    """
    own_client = http is None
    client = http or httpx.AsyncClient(timeout=timeout_seconds)
    try:
        webhook_id = str(uuid.uuid4())
        timestamp = str(int(time.time()))
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        signature = sign_body(secret, webhook_id=webhook_id, timestamp=timestamp, body=body)
        headers = {
            "Content-Type": "application/json",
            "webhook-id": webhook_id,
            "webhook-timestamp": timestamp,
            "webhook-signature": signature,
        }
        backoff = 0.5
        for attempt in range(1, max_retries + 1):
            try:
                resp = await client.post(url, content=body, headers=headers)
                if _HTTP_2XX_MIN <= resp.status_code < _HTTP_3XX_MIN:
                    return True
                if (
                    resp.status_code >= _HTTP_5XX_MIN
                    or resp.status_code == _HTTP_TOO_MANY
                ):
                    logger.info(
                        "notifications.webhook.retryable",
                        url=url,
                        status=resp.status_code,
                        attempt=attempt,
                    )
                else:
                    # 4xx — won't change on retry; give up immediately.
                    logger.warning(
                        "notifications.webhook.client_error",
                        url=url,
                        status=resp.status_code,
                    )
                    return False
            except Exception as exc:
                logger.info(
                    "notifications.webhook.send_error",
                    url=url,
                    error=str(exc),
                    attempt=attempt,
                )
            if attempt < max_retries:
                await asyncio.sleep(backoff)
                backoff *= 2.0
        logger.warning(
            "notifications.webhook.exhausted_retries",
            url=url,
            attempts=max_retries,
        )
        return False
    finally:
        if own_client:
            await client.aclose()
