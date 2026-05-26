"""Mock LOC server — FastAPI app driven by a JSON scenario file.

The scenario JSON describes per-endpoint canned responses. Every
inbound request is appended to the in-memory call log so the runner
can inspect what the SDK sent.

Endpoints implemented (mirror the real LOC API surface):

  POST /v1/jobs/mint              — mint a one-shot job
  POST /v1/jobs/{id}/settle       — settle a job (idempotent)
  POST /v1/sessions/open          — open a session
  POST /v1/sessions/{id}/refill   — refill a session
  POST /v1/sessions/{id}/close    — close a session
  POST /v1/telemetry              — telemetry ingest
  GET  /v1/sdk/manifest           — SDK approval manifest
  GET  /v1/sdk/manifest/pubkey    — manifest Ed25519 pubkey

Test-control surface:

  GET  /_test/inspect             — return the full call log
  POST /_test/reset               — clear the call log
  GET  /_test/health              — readiness probe
"""

from __future__ import annotations

import argparse
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
    """Build a fresh FastAPI app bound to the given scenario.

    Re-built per process so each conformance run starts from a clean
    call log.
    """
    app = FastAPI()
    state: dict[str, Any] = {
        "scenario": scenario,
        "calls": [],
        "started_at": time.time(),
    }

    loc_responses: dict[str, Any] = scenario.get("loc", {}).get("responses", {})

    def _match_response(method: str, path: str) -> dict[str, Any] | None:
        """Look up a canned response for ``METHOD path`` — supports
        ``{id}`` templating in scenario keys."""
        direct_key = f"{method} {path}"
        if direct_key in loc_responses:
            return loc_responses[direct_key]
        for key, resp in loc_responses.items():
            try:
                key_method, key_path = key.split(" ", 1)
            except ValueError:
                continue
            if key_method != method:
                continue
            # `{var}` matches any non-slash segment.
            pattern = "^" + re.sub(r"\{[^/}]+\}", r"[^/]+", key_path) + "$"
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
        try:
            body_bytes = await request.body()
            body_json: Any
            if body_bytes:
                # Telemetry batches are gzip-compressed above a size
                # threshold (per the SDK contract). Decompress before
                # JSON-parsing so the call log shows the actual events
                # for assertion.
                if request.headers.get("content-encoding") == "gzip":
                    import gzip as _gzip

                    try:
                        body_bytes = _gzip.decompress(body_bytes)
                    except Exception:
                        pass  # leave as-is and let json.loads fail
                try:
                    body_json = json.loads(body_bytes)
                except (ValueError, json.JSONDecodeError):
                    body_json = body_bytes.decode("utf-8", errors="replace")
            else:
                body_json = None
        except Exception:
            body_json = "<unread>"

        # Record the call (filtered headers — drop trivial ones that
        # bloat the log).
        headers: dict[str, str] = {}
        for k, v in request.headers.items():
            lk = k.lower()
            if lk in {
                "host",
                "user-agent",
                "accept",
                "accept-encoding",
                "content-length",
                "connection",
            }:
                continue
            headers[lk] = v

        state["calls"].append({
            "ts": time.time(),
            "method": method,
            "path": path,
            "query": dict(request.query_params),
            "headers": headers,
            "body": body_json,
        })

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

    # Route catch-alls for the LOC surface. FastAPI's auto-docs aren't
    # needed here; we just want a deterministic responder.
    @app.post("/v1/jobs")
    async def jobs_mint(request: Request) -> JSONResponse:
        return await _record_and_respond("POST", "/v1/jobs", request)

    @app.post("/v1/jobs/{job_id}/settle")
    async def jobs_settle(job_id: str, request: Request) -> JSONResponse:
        return await _record_and_respond("POST", f"/v1/jobs/{job_id}/settle", request)

    @app.post("/v1/sessions")
    async def sessions_open(request: Request) -> JSONResponse:
        return await _record_and_respond("POST", "/v1/sessions", request)

    @app.post("/v1/sessions/{session_id}/refill")
    async def sessions_refill(session_id: str, request: Request) -> JSONResponse:
        return await _record_and_respond(
            "POST", f"/v1/sessions/{session_id}/refill", request
        )

    @app.post("/v1/sessions/{session_id}/close")
    async def sessions_close(session_id: str, request: Request) -> JSONResponse:
        return await _record_and_respond(
            "POST", f"/v1/sessions/{session_id}/close", request
        )

    @app.post("/v1/telemetry")
    async def telemetry_ingest(request: Request) -> JSONResponse:
        return await _record_and_respond("POST", "/v1/telemetry", request)

    @app.get("/v1/sdk/manifest")
    async def sdk_manifest(request: Request) -> JSONResponse:
        return await _record_and_respond("GET", "/v1/sdk/manifest", request)

    @app.get("/v1/sdk/manifest/pubkey")
    async def sdk_manifest_pubkey(request: Request) -> JSONResponse:
        return await _record_and_respond("GET", "/v1/sdk/manifest/pubkey", request)

    return app


@contextmanager
def serve_in_background(scenario_path: str, *, host: str = "127.0.0.1", port: int = 0) -> Iterator[int]:
    """Spawn the mock LOC in a thread and yield the chosen port.

    For pytest fixtures inside the Python runner. External (TS/Go/Rust)
    runners launch via ``python -m conformance.mock_loc`` instead.
    """
    import threading

    scenario = _load_scenario(scenario_path)
    app = build_app(scenario)
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    # Wait for uvicorn to bind. Server.started flips True after startup.
    deadline = time.time() + 5.0
    while not server.started and time.time() < deadline:
        time.sleep(0.02)
    if not server.started:
        raise RuntimeError("mock LOC server failed to start within 5s")
    # uvicorn populates `server.servers[0].sockets[0].getsockname()` after start.
    chosen_port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield chosen_port
    finally:
        server.should_exit = True
        thread.join(timeout=2.0)


def main() -> None:
    parser = argparse.ArgumentParser(description="Mock LOC server for SDK conformance.")
    parser.add_argument("--scenario", required=True, help="Path to scenario JSON file.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()

    if not Path(args.scenario).is_file():
        print(f"scenario file not found: {args.scenario}", file=sys.stderr)
        sys.exit(2)

    scenario = _load_scenario(args.scenario)
    app = build_app(scenario)
    # Print port on stdout so external runners can read it.
    config = uvicorn.Config(app, host=args.host, port=args.port, log_level="warning")
    server = uvicorn.Server(config)

    # Print port after bind by patching `serve` → couldn't be simpler:
    # uvicorn supports `--port 0` and we just need the SDK runner to
    # learn the bound port. The easiest cross-language signal is:
    # honor the LOC_MOCK_PORT env var when set; otherwise dump a
    # JSON line `{"port": N}` to stdout once bound.
    fixed = os.environ.get("LOC_MOCK_PORT")
    if fixed:
        config.port = int(fixed)
        print(json.dumps({"port": int(fixed)}), flush=True)
        server.run()
        return

    import asyncio

    async def _runner() -> None:
        await server.startup()
        bound = server.servers[0].sockets[0].getsockname()[1]
        print(json.dumps({"port": bound}), flush=True)
        await server.main_loop()
        await server.shutdown()

    asyncio.run(_runner())


if __name__ == "__main__":  # pragma: no cover
    main()
