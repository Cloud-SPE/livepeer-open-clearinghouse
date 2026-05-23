"""Reference Python SDK for PymtHouse.

A thin, dependency-light wrapper around the gateway's HTTP API. Two
things to remember:

* Every call needs `X-API-Key: pymth_live_...`. Get one from the portal
  after operator approval.
* `mint_payment` returns base64 `payment_bytes` — put those verbatim
  into a `Livepeer-Payment` header on your request to the orchestrator.

See `example.py` for the full mint → orchestrator → reconcile flow.
"""

from pymthouse_sdk.client import (
    Mint,
    PymtHouseClient,
)
from pymthouse_sdk.errors import (
    AccountNotApproved,
    DaemonUnavailable,
    DuplicateRequest,
    InsufficientCredit,
    NoRouteAvailable,
    PymtHouseError,
    RateLimited,
)

__all__ = [
    "AccountNotApproved",
    "DaemonUnavailable",
    "DuplicateRequest",
    "InsufficientCredit",
    "Mint",
    "NoRouteAvailable",
    "PymtHouseClient",
    "PymtHouseError",
    "RateLimited",
]
