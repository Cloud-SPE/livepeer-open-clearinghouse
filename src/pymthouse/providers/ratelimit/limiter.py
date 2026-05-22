"""In-process token-bucket rate limiter.

A bucket per `(route, key)` pair (where `key` is normally the caller's IP).
Each bucket starts full at ``capacity`` tokens; one token is consumed per
request; the bucket refills at ``refill_per_minute / 60`` tokens per second.

Single-instance only — buckets live in process memory. When we go
multi-instance, swap this implementation for one backed by Redis without
changing the public Protocol.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass


@dataclass(slots=True)
class _Bucket:
    tokens: float
    last_refill_ts: float


class RateLimiter:
    """Process-wide in-memory token-bucket store."""

    def __init__(self) -> None:
        self._buckets: dict[tuple[str, str], _Bucket] = {}
        self._lock = asyncio.Lock()

    async def acquire(
        self,
        *,
        route: str,
        key: str,
        capacity: int,
        refill_per_minute: int,
    ) -> tuple[bool, int]:
        """Try to consume one token. Returns (allowed, retry_after_seconds).

        ``retry_after_seconds`` is 0 when allowed; otherwise the number of
        whole seconds until the bucket would accumulate one full token.
        """
        if capacity <= 0 or refill_per_minute <= 0:
            return True, 0  # disabled

        now = time.monotonic()
        refill_per_second = refill_per_minute / 60.0
        bucket_key = (route, key)

        async with self._lock:
            bucket = self._buckets.get(bucket_key)
            if bucket is None:
                bucket = _Bucket(tokens=float(capacity), last_refill_ts=now)
                self._buckets[bucket_key] = bucket
            else:
                elapsed = now - bucket.last_refill_ts
                bucket.tokens = min(
                    float(capacity), bucket.tokens + elapsed * refill_per_second
                )
                bucket.last_refill_ts = now

            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return True, 0

            # Need one full token: how many whole seconds until we'd have one?
            deficit = 1.0 - bucket.tokens
            retry_after_s = int(deficit / refill_per_second) + 1
            return False, max(retry_after_s, 1)

    def reset(self) -> None:
        """Drop all buckets. Useful in tests."""
        self._buckets.clear()
