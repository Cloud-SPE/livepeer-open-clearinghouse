"""Pydantic request/response types for the sessions domain."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class CreateSessionRequest(BaseModel):
    """Inbound: ``POST /v1/sessions``.

    The SDK declares its intent for a long-lived session: the
    capability + offering pair to bill against, the estimated runway
    (best-guess of what the session is likely to consume), and the
    absolute ceiling (``max_total_units``) that LOC will encumber up
    front under handoff-mode's worst-case sizing rule.

    ``max_total_units`` MUST be >= ``estimated_runway_units`` and > 0.
    """

    capability: str = Field(min_length=1)
    offering: str = Field(min_length=1)
    estimated_runway_units: int = Field(gt=0)
    max_total_units: int = Field(gt=0)


class CapStatus(BaseModel):
    """Cap headroom snapshot returned with every successful refill.

    All percentages are in ``[0.0, 1.0]``. ``None`` means the cap
    isn't enabled / configured (e.g., user has no spend-period cap
    set, or operator-pool cap is opt-in and disabled).

    ``will_refuse_next_refill`` flips ``true`` when any enabled cap
    is at or above the imminent-refusal threshold AND the projected
    next-mint would push it over. SDK uses this to surface a
    winddown warning to the customer one refill window early.

    ``winddown_reason`` is a short machine-readable string when
    ``will_refuse_next_refill=true``: ``"session_cap_imminent"``,
    ``"spend_period_cap_imminent"``, ``"user_balance_imminent"``,
    ``"operator_pool_cap_imminent"``. ``None`` otherwise.
    """

    session_pct_used: float = Field(ge=0.0, le=1.0)
    spend_period_pct_used: float | None = Field(default=None, ge=0.0, le=1.0)
    user_balance_pct_used: float | None = Field(default=None, ge=0.0, le=1.0)
    operator_pool_pct_used: float | None = Field(default=None, ge=0.0, le=1.0)
    will_refuse_next_refill: bool
    winddown_reason: str | None = None


class RefillSessionRequest(BaseModel):
    """Inbound: ``POST /v1/sessions/{id}/refill``.

    Body is mostly empty in v1 — the SDK signals "broker emitted
    Livepeer-Balance-Low, please mint more." The optional
    ``observed_consumed_units`` is an advisory hint from the SDK's
    view of the broker's debit ledger; LOC cross-checks via
    ``GetSessionDebits`` and uses the daemon's number as
    authoritative (per the trust model).
    """

    observed_consumed_units: int | None = Field(default=None, ge=0)


class RefillSessionResponse(BaseModel):
    """Outbound: ``POST /v1/sessions/{id}/refill`` success (200).

    Carries the newly-minted top-up envelope plus a fresh cap_status
    snapshot. The SDK delivers ``payment_envelope`` to the broker
    via the mode-specific channel (``session.topup`` JSON frame for
    ``session-control-plus-media@v0``; HTTP ``POST {topup_url}`` for
    the ``live-session-*`` modes).
    """

    work_id: str
    refill_seq: int
    payment_envelope: str
    expected_value_wei: int
    funded_value_wei: int
    cap_status: CapStatus


class CloseSessionRequest(BaseModel):
    """Inbound: ``POST /v1/sessions/{id}/close``.

    SDK reports the final actual_units consumed (read from the
    broker's ``Livepeer-Work-Units`` trailer or the equivalent
    in-band signal) and optionally the parsed ``SettlementRecord``
    from the broker if one was delivered.

    Per the trust model in the design doc, the SDK report is
    advisory; the payer-daemon's ``GetSessionDebits`` is the
    authoritative source. v1 trusts the SDK report on the synchronous
    close path; the reconciliation janitor (PR-8) does the daemon
    cross-check and corrects any divergence.
    """

    actual_units: int = Field(ge=0)
    outcome: str | None = None
    settlement: dict[str, Any] | None = None


class CloseSessionResponse(BaseModel):
    """Outbound: ``POST /v1/sessions/{id}/close``.

    Carries the final accounting: how much the customer was billed
    for the session, how much encumbered value is being refunded
    back to their balance, and the settlement outcome string.
    """

    session_id: uuid.UUID
    work_id: str
    actual_units: int
    billed_value_wei: int
    refund_wei: int
    outcome: str
    closed_at: datetime


class CreateSessionResponse(BaseModel):
    """Outbound: ``POST /v1/sessions``.

    Carries everything the SDK needs to open the broker-side session
    and bookkeep the LOC-side lifecycle. The ``payment_envelope``
    is base64-encoded wire-format Payment bytes — the SDK attaches
    it as the ``Livepeer-Payment`` HTTP header (or upgrade header,
    for WS modes) when connecting to ``broker_url``.

    Per exec-plan 002 handoff design, LOC never sits in the data
    path: ``broker_url`` is the orchestrator's HTTP/WS endpoint the
    SDK talks to directly. The two ``*_endpoint`` fields are
    LOC-relative paths the SDK calls when it needs to refill or
    explicitly close the session.
    """

    session_id: uuid.UUID
    work_id: str
    broker_url: str
    mode: str
    payment_envelope: str
    expected_value_wei: int
    funded_value_wei: int
    refill_endpoint: str
    close_endpoint: str
    opened_at: datetime
