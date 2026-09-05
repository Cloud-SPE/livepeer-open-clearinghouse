"""Pydantic request/response types for the sessions domain."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from livepeer_open_clearinghouse.providers.registry_daemon import RouteBinding, RouteSnapshot


class SessionAxesView(BaseModel):
    """Authoritative paid-session/v1 offering axes selected for the session."""

    model_config = ConfigDict(extra="allow")

    descriptor_schema: str = Field(pattern=r"^[a-z][a-z0-9-]*/v[0-9]+$")
    attachment: Literal["external"] = "external"
    metering: Literal["runner-reported"]
    refill: Literal["extensible", "bounded"] = "extensible"


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
    descriptor_schema: str = Field(pattern=r"^[a-z][a-z0-9-]*/v[0-9]+$")
    session_params: dict[str, Any] = Field(default_factory=dict)
    estimated_runway_units: int = Field(gt=0)
    max_total_units: int = Field(gt=0)
    route_binding: RouteBinding | None = None


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
    view of broker progress. Signed broker settlements, supplied on
    close/reconciliation, are authoritative for delivered work.
    """

    observed_consumed_units: int | None = Field(default=None, ge=0)

    # Present only after the broker returned `recipient_rotated` for a
    # previously issued LOC response. Both fields bind the rejected payment
    # before LOC evicts payer state and mints a successor.
    rebind_from: str | None = None
    replaces_request_id: str | None = None

    @model_validator(mode="after")
    def rotation_fields_are_atomic(self) -> RefillSessionRequest:
        if (self.rebind_from is None) != (self.replaces_request_id is None):
            raise ValueError("rebind_from and replaces_request_id must be supplied together")
        return self


class RefillSessionResponse(BaseModel):
    """Outbound: ``POST /v1/sessions/{id}/refill`` success (200).

    Carries the newly-minted top-up envelope plus a fresh cap_status
    snapshot. The SDK delivers ``payment_envelope`` through the authoritative
    paid-session HTTP top-up URL; a control WebSocket is only an optional push
    mirror.
    """

    work_id: str
    request_id: str
    refill_seq: int
    payment_envelope: str
    expected_value_wei: int
    funded_value_wei: int
    cap_status: CapStatus
    rebind_from: str | None = None


class SessionStatusResponse(BaseModel):
    """Outbound: ``GET /v1/sessions/{id}``.

    Customer-facing read-only view of a session's current state +
    running totals. Powers the SDK's ``session.status`` callback and
    the portal's per-session detail page. Always-on; no auth beyond
    the standard CurrentApiKey ownership check.

    For active sessions, ``cap_status`` carries the same shape the
    refill endpoint returns so portal UIs can show "how close to
    cap" without forcing a refill. For closed sessions, ``cap_status``
    is omitted (irrelevant) and the close fields are populated.
    """

    session_id: uuid.UUID
    work_id: str
    capability: str
    offering: str
    protocol: str
    state: str
    estimated_units: int
    max_total_units: int
    funded_value_wei: int
    billed_value_wei: int  # cumulative across all minted tickets (live) OR final billed (closed)
    refill_count: int
    cap_status: CapStatus | None
    opened_at: datetime
    closed_at: datetime | None
    actual_units: int | None
    outcome: str | None


class SessionSettlementSignature(BaseModel):
    model_config = ConfigDict(extra="forbid")

    algorithm: Literal["secp256k1"]
    canonicalization: Literal["jcs"]
    value: str = Field(pattern=r"^0x[0-9a-fA-F]{130}$")


class SessionSettlementEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    payload: dict[str, Any]
    signature: SessionSettlementSignature


class CloseSessionRequest(BaseModel):
    """Inbound: ``POST /v1/sessions/{id}/close``.

    SDK forwards the required broker-signed terminal settlement. The
    reported units and optional outcome are consistency assertions;
    signed settlement fields are authoritative for accounting.
    """

    actual_units: int = Field(ge=0)
    outcome: str | None = None
    settlement: SessionSettlementEnvelope


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
    it as the ``Livepeer-Payment`` HTTP header when opening the broker's
    paid-session/v1 control resource.

    Per exec-plan 002 handoff design, LOC never sits in the data
    path: ``broker_url`` is the orchestrator's HTTP/WS endpoint the
    SDK talks to directly. The two ``*_endpoint`` fields are
    LOC-relative paths the SDK calls when it needs to refill or
    explicitly close the session.
    """

    session_id: uuid.UUID
    request_id: str
    work_id: str
    broker_url: str
    protocol: str
    session: SessionAxesView
    route_snapshot: RouteSnapshot
    payment_envelope: str
    expected_value_wei: int
    funded_value_wei: int
    refill_endpoint: str
    close_endpoint: str
    opened_at: datetime
