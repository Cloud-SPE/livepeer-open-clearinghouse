"""Reference Python SDK for Livepeer Open Clearinghouse (handoff mode).

A thin, dependency-light wrapper around the gateway's HTTP API.

Two flows:

* ``submit_job`` — atomic / post-settled / streaming work (cases a/b/c).
  Single function call that mints via ``POST /v1/jobs``, talks to the
  broker directly, reads ``Livepeer-Work-Units`` from the response,
  and settles via ``POST /v1/jobs/{id}/settle``.

* ``open_session`` — long-running interactive work (case d). Returns a
  ``SessionHandle`` carrying the broker URL + minted envelope; SDK
  consumer drives the broker WS / RTMP wire today. Companion
  ``refill_session`` and ``close_session`` helpers cover the LOC-side
  refill / close calls.

Every call needs ``X-API-Key: pymth_live_...`` — get one from the
portal after operator approval. The SDK adds
``Livepeer-Open-Clearinghouse-SDK`` automatically for operator-side
trust scoring.

See ``example.py`` for end-to-end usage.
"""

from livepeer_open_clearinghouse_sdk.client import (
    SDK_IDENTITY,
    Capability,
    CapStatus,
    JobResult,
    Offering,
    OpenClearinghouseClient,
    Orchestrator,
    RouteView,
    SessionHandle,
    is_open_clearinghouse_error,
    wei_to_eth,
)
from livepeer_open_clearinghouse_sdk.session_runner import (
    BOUNDED_MODES,
    HTTP_TOPUP_MODES,
    WS_TOPUP_MODES,
    RefillEvent,
    SessionRunner,
    WinddownEvent,
)
from livepeer_open_clearinghouse_sdk.errors import (
    AccountNotApproved,
    DaemonUnavailable,
    DuplicateRequest,
    InsufficientCredit,
    NoRouteAvailable,
    OpenClearinghouseError,
    RateLimited,
)

__all__ = [
    "BOUNDED_MODES",
    "HTTP_TOPUP_MODES",
    "SDK_IDENTITY",
    "WS_TOPUP_MODES",
    "AccountNotApproved",
    "CapStatus",
    "Capability",
    "DaemonUnavailable",
    "DuplicateRequest",
    "InsufficientCredit",
    "JobResult",
    "NoRouteAvailable",
    "Offering",
    "OpenClearinghouseClient",
    "OpenClearinghouseError",
    "Orchestrator",
    "RateLimited",
    "RefillEvent",
    "RouteView",
    "SessionHandle",
    "SessionRunner",
    "WinddownEvent",
    "is_open_clearinghouse_error",
    "wei_to_eth",
]
