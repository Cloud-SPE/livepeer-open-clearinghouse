"""Unit tests for spend-window boundary math."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from livepeer_open_clearinghouse.domains.billing.service import window_bounds_for


@pytest.mark.unit
def test_window_is_aligned_to_period_boundary() -> None:
    # 1-hour period
    now = datetime(2026, 5, 22, 13, 27, 31, tzinfo=UTC)
    start, end = window_bounds_for(now, period_seconds=3600)
    assert start == datetime(2026, 5, 22, 13, 0, 0, tzinfo=UTC)
    assert end == datetime(2026, 5, 22, 14, 0, 0, tzinfo=UTC)


@pytest.mark.unit
def test_consecutive_times_in_same_window_share_bounds() -> None:
    a = datetime(2026, 5, 22, 1, 0, 0, tzinfo=UTC)
    b = a + timedelta(seconds=30)
    sa, ea = window_bounds_for(a, 60)
    sb, eb = window_bounds_for(b, 60)
    assert sa == sb
    assert ea == eb


@pytest.mark.unit
def test_window_rolls_at_boundary() -> None:
    a = datetime(2026, 5, 22, 0, 59, 59, tzinfo=UTC)
    b = a + timedelta(seconds=2)
    sa, _ = window_bounds_for(a, 60)
    sb, _ = window_bounds_for(b, 60)
    assert sa != sb
    assert sb - sa == timedelta(seconds=60)


@pytest.mark.unit
def test_naive_datetime_rejected() -> None:
    with pytest.raises(ValueError):
        window_bounds_for(datetime(2026, 5, 22, 13, 0, 0), period_seconds=60)
