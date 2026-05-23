"""Unit tests for the token-bucket rate limiter."""

from __future__ import annotations

import asyncio

import pytest

from livepeer_open_clearinghouse.providers.ratelimit import RateLimiter


@pytest.mark.unit
async def test_capacity_zero_disables_limiter() -> None:
    rl = RateLimiter()
    for _ in range(100):
        allowed, retry = await rl.acquire(route="x", key="ip", capacity=0, refill_per_minute=0)
        assert allowed is True
        assert retry == 0


@pytest.mark.unit
async def test_first_n_requests_pass_then_throttle() -> None:
    rl = RateLimiter()
    for _ in range(3):
        allowed, _ = await rl.acquire(
            route="login", key="1.1.1.1", capacity=3, refill_per_minute=60
        )
        assert allowed is True
    # bucket empty
    allowed, retry = await rl.acquire(
        route="login", key="1.1.1.1", capacity=3, refill_per_minute=60
    )
    assert allowed is False
    assert retry >= 1


@pytest.mark.unit
async def test_separate_keys_have_separate_buckets() -> None:
    rl = RateLimiter()
    for _ in range(2):
        allowed, _ = await rl.acquire(route="login", key="a", capacity=2, refill_per_minute=60)
        assert allowed is True
    # `a` is out, but `b` still has a full bucket
    allowed, _ = await rl.acquire(route="login", key="b", capacity=2, refill_per_minute=60)
    assert allowed is True


@pytest.mark.unit
async def test_separate_routes_have_separate_buckets() -> None:
    rl = RateLimiter()
    for _ in range(2):
        allowed, _ = await rl.acquire(route="login", key="x", capacity=2, refill_per_minute=60)
        assert allowed is True
    allowed, _ = await rl.acquire(route="signup", key="x", capacity=2, refill_per_minute=60)
    assert allowed is True  # different route -> different bucket


@pytest.mark.unit
async def test_refill_eventually_unblocks() -> None:
    rl = RateLimiter()
    # capacity=1, refill 60/min = 1/sec
    allowed, _ = await rl.acquire(route="r", key="x", capacity=1, refill_per_minute=60)
    assert allowed is True
    allowed, _ = await rl.acquire(route="r", key="x", capacity=1, refill_per_minute=60)
    assert allowed is False
    # Wait for ~1.1s so the bucket refills by ~1 token.
    await asyncio.sleep(1.1)
    allowed, _ = await rl.acquire(route="r", key="x", capacity=1, refill_per_minute=60)
    assert allowed is True


@pytest.mark.unit
async def test_retry_after_is_at_least_one_second() -> None:
    rl = RateLimiter()
    # Drain a 1-capacity bucket
    await rl.acquire(route="r", key="x", capacity=1, refill_per_minute=60)
    allowed, retry = await rl.acquire(route="r", key="x", capacity=1, refill_per_minute=60)
    assert allowed is False
    assert retry >= 1
