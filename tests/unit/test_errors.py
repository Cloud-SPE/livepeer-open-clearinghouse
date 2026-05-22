"""Unit tests for structured PymtHouse errors."""

from __future__ import annotations

import pytest

from pymthouse.errors import (
    AccountNotApproved,
    DaemonUnavailable,
    DuplicateRequest,
    InsufficientCredit,
    NoRouteAvailable,
    PymtHouseError,
    SpendCapExceeded,
)


@pytest.mark.unit
def test_insufficient_credit_carries_amounts_in_details() -> None:
    e = InsufficientCredit(available_wei=12_000, required_wei=50_000)
    assert isinstance(e, PymtHouseError)
    assert e.status_code == 402
    assert e.code == "INSUFFICIENT_CREDIT"
    assert e.details["available_wei"] == "12000"
    assert e.details["required_wei"] == "50000"


@pytest.mark.unit
def test_spend_cap_exceeded() -> None:
    e = SpendCapExceeded(cap_wei=1_000_000, would_be_spent_wei=1_500_000)
    assert e.status_code == 402
    assert e.code == "SPEND_CAP_EXCEEDED"


@pytest.mark.unit
def test_account_not_approved_carries_no_details() -> None:
    e = AccountNotApproved()
    assert e.status_code == 403
    assert e.code == "ACCOUNT_NOT_APPROVED"
    assert e.details == {}


@pytest.mark.unit
def test_no_route_available_includes_lookup_keys() -> None:
    e = NoRouteAvailable(capability="x", offering="y")
    assert e.status_code == 404
    assert e.details["capability"] == "x"
    assert e.details["offering"] == "y"


@pytest.mark.unit
def test_daemon_unavailable() -> None:
    e = DaemonUnavailable(daemon="payment-daemon", reason="deposit insufficient")
    assert e.status_code == 503
    assert e.details["daemon"] == "payment-daemon"


@pytest.mark.unit
def test_duplicate_request() -> None:
    e = DuplicateRequest()
    assert e.status_code == 409
    assert e.code == "DUPLICATE_REQUEST"
