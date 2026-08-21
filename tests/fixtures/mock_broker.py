"""In-tree mock broker fixture for end-to-end SDK + LOC smoke tests.

A tiny FastAPI app that pretends to be an orchestrator-side
capability-broker. Accepts ``POST /v1/job`` and returns:

  - 200 OK with a stubbed JSON body
  - ``Livepeer-Work-Units`` header with whatever the caller asks for
    (via the ``X-Mock-Actual-Units`` request header), defaulting to
    half the estimated units in the request body's
    ``estimated_units`` field, or a configured constant.

Not wire-compatible with a real orchestrator. Useful for proving
the SDK / LOC handoff loop without paying real network costs.

Usage in pytest::

    from tests.fixtures.mock_broker import build_mock_broker_app


    @pytest_asyncio.fixture()
    async def broker():
        app = build_mock_broker_app(default_units=42)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://mock-broker",
        ) as client:
            yield client
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import FastAPI, Header, Request


def build_mock_broker_app(
    *,
    default_units: int = 42,
    default_status: int = 200,
    return_settlement: bool = False,
) -> FastAPI:
    """Construct a FastAPI app that mimics a capability-broker.

    Knobs:

      - ``default_units``: returned in the ``Livepeer-Work-Units``
        response header unless the request overrides via
        ``X-Mock-Actual-Units``.
      - ``default_status``: response status (200 by default; bump to
        429/503 for failure-injection tests).
      - ``return_settlement``: if true, attach a ``Livepeer-Settlement``
        header with a stub base64 payload the SDK can pass through.

    The app records every request on ``app.state.requests`` so tests
    can inspect headers, body, and timing after the fact.
    """
    app = FastAPI(title="mock-broker")
    app.state.requests = []

    @app.post("/v1/job")
    async def handle_job(
        request: Request,
        livepeer_payment: Annotated[str | None, Header(alias="Livepeer-Payment")] = None,
        livepeer_capability: Annotated[str | None, Header(alias="Livepeer-Capability")] = None,
        livepeer_offering: Annotated[str | None, Header(alias="Livepeer-Offering")] = None,
        livepeer_protocol: Annotated[str | None, Header(alias="Livepeer-Protocol")] = None,
        livepeer_request_id: Annotated[str | None, Header(alias="Livepeer-Request-Id")] = None,
        x_mock_actual_units: Annotated[str | None, Header(alias="X-Mock-Actual-Units")] = None,
    ) -> Any:
        body_bytes = await request.body()
        record = {
            "headers": dict(request.headers),
            "body_bytes": body_bytes,
            "capability": livepeer_capability,
            "offering": livepeer_offering,
            "protocol": livepeer_protocol,
            "request_id": livepeer_request_id,
            "had_payment_header": livepeer_payment is not None,
        }
        app.state.requests.append(record)

        actual_units = int(x_mock_actual_units) if x_mock_actual_units else default_units
        from fastapi.responses import JSONResponse

        broker_job_id = f"mock-{livepeer_request_id or 'no-req-id'}"
        headers: dict[str, str] = {
            "Livepeer-Work-Units": str(actual_units),
            "Livepeer-Work-Unit": "token",
            "Livepeer-Job-Id": broker_job_id,
        }
        if return_settlement:
            # Stub base64 JSON: {"outcome":"OVERFUNDED","actual_units":<n>}
            import base64
            import json

            settlement = base64.b64encode(
                json.dumps({"outcome": "OVERFUNDED", "actual_units": actual_units}).encode()
            ).decode()
            headers["Livepeer-Settlement"] = settlement

        # Stub reply body — capability-shaped enough to round-trip.
        reply: dict[str, Any] = {
            "id": broker_job_id,
            "object": "mock.response",
            "model": livepeer_offering,
            "usage": {"actual_units": actual_units},
        }
        return JSONResponse(reply, status_code=default_status, headers=headers)

    @app.post("/v1/cap/{broker_session_id}/topup")
    async def handle_topup(
        broker_session_id: str,
        request: Request,
        livepeer_payment: Annotated[str | None, Header(alias="Livepeer-Payment")] = None,
    ) -> Any:
        body_bytes = await request.body()
        app.state.requests.append(
            {
                "path": f"/v1/cap/{broker_session_id}/topup",
                "headers": dict(request.headers),
                "body_bytes": body_bytes,
                "had_payment_header": livepeer_payment is not None,
            }
        )
        return {
            "broker_session_id": broker_session_id,
            "work_id": "mock-work-id",
            "state": "publishing",
            "balance": {
                "status": "ok",
                "runway_seconds_estimate": 184,
            },
        }

    return app
