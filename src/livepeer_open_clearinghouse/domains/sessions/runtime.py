"""FastAPI routes for the sessions domain.

Skeleton router only — the actual session lifecycle endpoints land
in Phase 2 (PR-4+) per exec-plan 002. The router is wired into the
app composition root now so subsequent PRs only need to add handlers,
not also touch the app wiring.

Endpoints to land in Phase 2:

  * ``POST   /v1/sessions``                 — open a session
  * ``POST   /v1/sessions/{id}/refill``     — mint a top-up
  * ``POST   /v1/sessions/{id}/close``      — explicit close
  * ``GET    /v1/sessions/{id}``            — status / balance (customer)
  * ``POST   /v1/jobs/{id}/settle``         — single-shot settlement

See ``docs/exec-plans/active/002-long-running-sessions.md`` for the
contract each handler must satisfy.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/v1", tags=["sessions"])
