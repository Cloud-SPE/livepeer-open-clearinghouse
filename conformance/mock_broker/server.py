"""Mock broker server — FastAPI, scenario-driven.

Mirrors the mock_loc layout: per-endpoint canned responses, a call
log, and a ``/_test/*`` control surface. Same JSON scenario file
loaded by mock_loc — the runner just hands the same scenario to
both processes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


def _load_scenario(path: str) -> dict[str, Any]:
    with open(path) as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise SystemExit(f"scenario {path} must be a JSON object")
    return data


def build_app(scenario: dict[str, Any]) -> FastAPI:
    app = FastAPI()
    state: dict[str, Any] = {
        "scenario": scenario,
        "calls": [],
        "started_at": time.time(),
    }

    broker_responses: dict[str, Any] = scenario.get("broker", {}).get("responses", {})

    def _match_response(method: str, path: str) -> dict[str, Any] | None:
        direct = f"{method} {path}"
        if direct in broker_responses:
            return broker_responses[direct]
        for key, resp in broker_responses.items():
            try:
                k_method, k_path = key.split(" ", 1)
            except ValueError:
                continue
            if k_method != method:
                continue
            pattern = "^" + re.sub(r"\{[^/}]+\}", r"[^/]+", k_path) + "$"
            if re.match(pattern, path):
                return resp
        return None

    @app.get("/_test/health")
    async def _health() -> dict[str, Any]:
        return {"ok": True, "started_at": state["started_at"]}

    @app.get("/_test/inspect")
    async def _inspect() -> dict[str, Any]:
        return {
            "scenario_id": scenario.get("id"),
            "calls": list(state["calls"]),
        }

    @app.post("/_test/reset")
    async def _reset() -> dict[str, bool]:
        state["calls"] = []
        return {"ok": True}

    async def _record_and_respond(method: str, path: str, request: Request) -> JSONResponse:
        body_bytes = await request.body()
        body_json: Any
        if body_bytes:
            if request.headers.get("content-encoding") == "gzip":
                import gzip as _gzip

                try:
                    body_bytes = _gzip.decompress(body_bytes)
                except Exception:
                    pass
            try:
                body_json = json.loads(body_bytes)
            except (ValueError, json.JSONDecodeError):
                body_json = body_bytes.decode("utf-8", errors="replace")
        else:
            body_json = None

        headers = {
            k.lower(): v
            for k, v in request.headers.items()
            if k.lower()
            not in {
                "host",
                "user-agent",
                "accept",
                "accept-encoding",
                "content-length",
                "connection",
            }
        }

        state["calls"].append(
            {
                "ts": time.time(),
                "method": method,
                "path": path,
                "query": dict(request.query_params),
                "headers": headers,
                "body": body_json,
            }
        )

        resp_spec = _match_response(method, path)
        if resp_spec is None:
            return JSONResponse(
                status_code=404,
                content={
                    "error": {
                        "code": "no_mock_response",
                        "message": f"no canned response for {method} {path}",
                    }
                },
            )
        status = int(resp_spec.get("status", 200))
        body = resp_spec.get("body", {})
        out_headers = {str(k): str(v) for k, v in resp_spec.get("headers", {}).items()}
        return JSONResponse(status_code=status, content=body, headers=out_headers)

    @app.post("/v1/job")
    async def job(request: Request) -> JSONResponse:
        return await _record_and_respond("POST", "/v1/job", request)

    @app.get("/v1/settlement/{identifier}")
    async def settlement(identifier: str, request: Request) -> JSONResponse:
        return await _record_and_respond("GET", f"/v1/settlement/{identifier}", request)

    @app.post("/v1/session")
    async def session_open(request: Request) -> JSONResponse:
        return await _record_and_respond("POST", "/v1/session", request)

    @app.post("/v1/cap/{session_id}/topup")
    async def cap_topup(session_id: str, request: Request) -> JSONResponse:
        return await _record_and_respond("POST", f"/v1/cap/{session_id}/topup", request)

    @app.post("/v1/cap/{session_id}/end")
    async def cap_end(session_id: str, request: Request) -> JSONResponse:
        return await _record_and_respond("POST", f"/v1/cap/{session_id}/end", request)

    @app.post("/topup")
    async def topup_root(request: Request) -> JSONResponse:
        # Many scenarios advertise control.topup_url as
        # http://broker/topup — keep a top-level alias so the SDK can
        # POST without LOC routing involvement.
        return await _record_and_respond("POST", "/topup", request)

    return app


@contextmanager
def serve_in_background(
    scenario_path: str, *, host: str = "127.0.0.1", port: int = 0
) -> Iterator[int]:
    import threading

    scenario = _load_scenario(scenario_path)
    app = build_app(scenario)
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 5.0
    while not server.started and time.time() < deadline:
        time.sleep(0.02)
    if not server.started:
        raise RuntimeError("mock broker server failed to start within 5s")
    chosen_port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield chosen_port
    finally:
        server.should_exit = True
        thread.join(timeout=2.0)


def main() -> None:
    parser = argparse.ArgumentParser(description="Mock broker server for SDK conformance.")
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()

    if not Path(args.scenario).is_file():
        print(f"scenario file not found: {args.scenario}", file=sys.stderr)
        sys.exit(2)

    scenario = _load_scenario(args.scenario)
    app = build_app(scenario)
    fixed = os.environ.get("BROKER_MOCK_PORT")
    config = uvicorn.Config(
        app, host=args.host, port=(int(fixed) if fixed else args.port), log_level="warning"
    )
    server = uvicorn.Server(config)

    async def _runner() -> None:
        await server.startup()
        bound = server.servers[0].sockets[0].getsockname()[1]
        print(json.dumps({"port": bound}), flush=True)
        await server.main_loop()
        await server.shutdown()

    asyncio.run(_runner())


if __name__ == "__main__":  # pragma: no cover
    main()
