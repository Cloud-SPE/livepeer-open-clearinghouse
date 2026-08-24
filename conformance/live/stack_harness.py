"""Boot the hermetic real-process LOC + Modules v2 stack.

The chain address-to-manifest lookup and the workload backend are the only
fakes. Registry, payer, payee, broker, Postgres, and LOC all run as their real
processes and communicate over their production HTTP, PostgreSQL, and UDS
boundaries.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import traceback
import urllib.request
import uuid
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from http.server import BaseHTTPRequestHandler, SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import IO, Any, ClassVar

import asyncpg
import grpc
import httpx
from livepeer_open_clearinghouse_sdk import OpenClearinghouseClient
from livepeer_open_clearinghouse_sdk.session_runner import SessionRunner
from registry_seed_probe import (  # type: ignore[import-not-found]
    COLD_KEY,
    SETTLEMENT_KEY,
    _signed_manifest,
)
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from livepeer_open_clearinghouse._gen.livepeer.payments.v1 import (
    payee_admin_pb2,
    payee_admin_pb2_grpc,
    types_pb2,
)
from livepeer_open_clearinghouse.domains.accounts.repo import User
from livepeer_open_clearinghouse.domains.admin import repo as _admin_repo  # noqa: F401
from livepeer_open_clearinghouse.domains.api_keys import service as api_keys_service
from livepeer_open_clearinghouse.domains.billing.repo import CreditBalance, CreditLedger
from livepeer_open_clearinghouse.domains.payments import repo as _payments_repo  # noqa: F401
from livepeer_open_clearinghouse.domains.sessions import repo as _sessions_repo  # noqa: F401
from livepeer_open_clearinghouse.providers.registry_daemon import GrpcRegistryClient

_PAYEE_ADMIN_TOKEN = "live-matrix-payee-admin"  # noqa: S105 — disposable harness secret


class _BackendHandler(BaseHTTPRequestHandler):
    job_calls = 0
    sessions: ClassVar[dict[str, str]] = {}
    block_next_job = False
    blocked_job_entered = threading.Event()
    blocked_job_release = threading.Event()

    def log_message(self, _format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._json({"ready": True})
            return
        if self.path.startswith("/sessions/"):
            session_id = self.path.removeprefix("/sessions/")
            self._json(
                {
                    "runner_session_id": session_id,
                    "state": type(self).sessions.get(session_id, "gone"),
                }
            )
            return
        self.send_error(404)

    def do_POST(self) -> None:
        if self.path == "/count":
            length = int(self.headers.get("Content-Length", "0"))
            if length:
                self.rfile.read(length)
            type(self).job_calls += 1
            if type(self).block_next_job:
                type(self).block_next_job = False
                type(self).blocked_job_entered.set()
                if not type(self).blocked_job_release.wait(timeout=10):
                    self.send_error(504)
                    return
            self._json({"bark_count": 1})
            return
        if self.path == "/sessions":
            length = int(self.headers.get("Content-Length", "0"))
            if length:
                self.rfile.read(length)
            session_id = f"runner_{uuid.uuid4()}"
            type(self).sessions[session_id] = "active"
            self._json(
                {
                    "runner_session_id": session_id,
                    "runtime": {
                        "schema": "test-runtime/v1",
                        "public": {"endpoint": f"https://runtime.invalid/{session_id}"},
                    },
                }
            )
            return
        self.send_error(404)

    def do_DELETE(self) -> None:
        if self.path.startswith("/sessions/"):
            session_id = self.path.removeprefix("/sessions/")
            type(self).sessions.pop(session_id, None)
            self._json({"terminated": True})
            return
        self.send_error(404)

    def _json(self, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, _format: str, *args: object) -> None:
        return


@dataclass(slots=True)
class _RunningProcess:
    name: str
    process: subprocess.Popen[str]
    log_file: IO[str]
    log_path: Path

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.log_file.close()

    def assert_running(self) -> None:
        code = self.process.poll()
        if code is not None:
            self.log_file.flush()
            log = self.log_path.read_text(errors="replace")
            raise RuntimeError(f"{self.name} exited with {code}:\n{log}")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for(
    description: str,
    predicate: Callable[[], bool],
    processes: list[_RunningProcess],
    *,
    timeout: float = 30,
) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        for process in processes:
            process.assert_running()
        try:
            if predicate():
                return
        except Exception as exc:  # readiness failures are expected while booting
            last_error = exc
        time.sleep(0.2)
    suffix = f": {last_error}" if last_error else ""
    raise TimeoutError(f"timed out waiting for {description}{suffix}")


def _http_json(url: str) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=2) as response:  # noqa: S310 — local harness URL
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise TypeError(f"{url} returned {type(payload).__name__}, want object")
    return payload


def _job_exchange(broker_url: str, request_id: str) -> dict[str, Any]:
    response = httpx.get(f"{broker_url.rstrip('/')}/v1/exchange/{request_id}", timeout=5)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise TypeError("broker exchange lookup did not return an object")
    return payload


def _start_process(
    name: str,
    command: list[str],
    artifacts: Path,
    processes: list[_RunningProcess],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
) -> _RunningProcess:
    log_path = artifacts / f"{name}.log"
    log_file = log_path.open("w")
    process = subprocess.Popen(  # noqa: S603
        command,
        cwd=cwd,
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        text=True,
    )
    running = _RunningProcess(name, process, log_file, log_path)
    processes.append(running)
    return running


def _build_go_binary(module: Path, package: str, output: Path, log_path: Path) -> None:
    go = shutil.which("go")
    if go is None:
        raise RuntimeError("go is unavailable")
    with log_path.open("w") as log:
        subprocess.run(  # noqa: S603 — fixed tool with harness-owned arguments
            [go, "build", "-o", str(output), package],
            cwd=module,
            check=True,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
        )


def _git_state(repo: Path) -> dict[str, Any]:
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git is unavailable")
    revision = subprocess.run(  # noqa: S603 — fixed git diagnostic
        [git, "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    dirty = subprocess.run(  # noqa: S603 — fixed git diagnostic
        [git, "status", "--porcelain"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return {"revision": revision, "dirty": bool(dirty)}


async def _postgres_ready(database_url: str) -> bool:
    connection = await asyncpg.connect(
        database_url.replace("postgresql+asyncpg://", "postgresql://")
    )
    await connection.close()
    return True


async def _select_route(socket_path: Path) -> dict[str, Any]:
    client = GrpcRegistryClient(str(socket_path))
    try:
        route = await client.select("test:job", "default")
        if route is None:
            raise AssertionError("LOC registry client received no test:job/default route")
        if route.protocol != "paid-job/v1":
            raise AssertionError(f"protocol={route.protocol!r}, want paid-job/v1")
        if route.units_per_price != 1000:
            raise AssertionError(f"units_per_price={route.units_per_price}, want 1000")
        if route.extra.get("job", {}).get("transports") != ["unary", "stream"]:
            raise AssertionError(f"job axes missing from route: {route.extra!r}")
        expected_key = "0x04" + SETTLEMENT_KEY.public_key.to_bytes().hex()
        if not route.settlement_keys or route.settlement_keys[0].public_key != expected_key:
            raise AssertionError("settlement delegation missing from selected route")
        session_route = await client.select("test:session", "default")
        if session_route is None or session_route.protocol != "paid-session/v1":
            raise AssertionError("LOC registry client received no paid-session/v1 route")
        if session_route.extra.get("session", {}).get("descriptor_schema") != "test-runtime/v1":
            raise AssertionError(f"session axes missing from route: {session_route.extra!r}")
        return {
            "protocol": route.protocol,
            "worker_url": route.worker_url,
            "work_unit": route.work_unit,
            "units_per_price": route.units_per_price,
            "settlement_public_key": expected_key,
        }
    finally:
        await client.close()


def _write_broker_config(
    path: Path,
    *,
    paid_port: int,
    metrics_port: int,
    payee_socket: Path,
    backend_url: str,
    settlement_key_path: Path,
    session_store_path: Path,
    sealing_key_path: Path,
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    path.write_text(
        f"""identity:
  orch_eth_address: "{COLD_KEY.public_key.to_address()}"
  label: "loc-live-harness"
  settlement_key_file: "{settlement_key_path}"
  settlement_key_not_before: "{(now - timedelta(hours=1)).isoformat().replace("+00:00", "Z")}"
  settlement_key_expires_at: "{(now + timedelta(hours=23)).isoformat().replace("+00:00", "Z")}"
external_base_url: "http://127.0.0.1:{paid_port}"
listen:
  paid: "127.0.0.1:{paid_port}"
  metrics: "127.0.0.1:{metrics_port}"
payment_daemon:
  socket: "{payee_socket}"
session_store:
  path: "{session_store_path}"
  sealing_key_file: "{sealing_key_path}"
  job_retention: 96h
capabilities:
  - id: "test:job"
    offering_id: "default"
    protocol: "paid-job/v1"
    job:
      transports: [unary, stream]
    work_unit:
      name: "tokens"
      extractor: {{type: "response-jsonpath", path: "$.bark_count"}}
    health:
      initial_status: "ready"
      probe:
        type: "http-status"
        interval_ms: 500
        timeout_ms: 250
        unhealthy_after: 2
        healthy_after: 1
        config:
          url: "{backend_url}/healthz"
    price:
      amount_wei: "100"
      per_units: 1000
    backend:
      transport: "http"
      url: "{backend_url}/count"
      auth: "none"
  - id: "test:session"
    offering_id: "default"
    protocol: "paid-session/v1"
    session:
      descriptor_schema: "test-runtime/v1"
      lease_policy: fixed
      lease_max_seconds: 600
      runner:
        create_path: /sessions
        status_path: "/sessions/{{id}}"
        terminate_path: "/sessions/{{id}}"
    health:
      initial_status: "ready"
    work_unit:
      name: "seconds"
    price:
      amount_wei: "200"
      per_units: 1000
    backend:
      transport: "http"
      url: "{backend_url}"
      auth: "none"
"""
    )


async def _seed_customer(database_url: str, *, pepper: str) -> str:
    """Create one funded SDK principal through LOC's real persistence layer."""

    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    try:
        async with factory() as db:
            user = User(
                email=f"live-matrix-{uuid.uuid4().hex}@example.test",
                email_verified_at=now,
                password_hash=None,
            )
            db.add(user)
            await db.flush()
            _key, raw_key = await api_keys_service.create(
                db,
                user_id=user.id,
                label="live-matrix",
                pepper=pepper,
            )
            initial_credit = 10**12
            db.add(CreditBalance(user_id=user.id, amount_wei=initial_credit))
            db.add(
                CreditLedger(
                    user_id=user.id,
                    delta_wei=initial_credit,
                    reason="topup",
                    related_payment_id=None,
                    related_topup_id=None,
                    created_by_operator_id=None,
                )
            )
            await db.commit()
            return raw_key
    finally:
        await engine.dispose()


def _broker_headers(job: dict[str, Any]) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Livepeer-Capability": "test:job",
        "Livepeer-Offering": "default",
        "Livepeer-Payment": str(job["payment_envelope"]),
        "Livepeer-Protocol": str(job["protocol"]),
        "Livepeer-Request-Id": str(job["request_id"]),
    }


async def _exercise_job_matrix(  # noqa: PLR0912 — one readable protocol matrix
    loc_url: str, api_key: str
) -> list[dict[str, Any]]:
    """Drive paid-job/v1 through public LOC and broker HTTP boundaries."""

    cases: list[dict[str, Any]] = []
    async with OpenClearinghouseClient(base_url=loc_url, api_key=api_key) as sdk:
        result = await sdk.submit_job(
            capability="test:job",
            offering="default",
            estimated_units=10_250,
            max_total_units=10_250,
            body={"prompt": "live SDK matrix"},
            request_id=f"sdk-{uuid.uuid4()}",
        )
        if result.status != 200 or result.actual_units != 1 or result.work_unit != "tokens":
            raise AssertionError(f"official SDK paid-job result is invalid: {result!r}")
        cases.append(
            {
                "case": "python_sdk_paid_job",
                "status": "passed",
                "request_id": result.request_id,
                "broker_job_id": result.broker_job_id,
            }
        )

    headers = {"X-API-Key": api_key, "Livepeer-Open-Clearinghouse-SDK": "live-matrix"}
    async with httpx.AsyncClient(base_url=loc_url, headers=headers, timeout=10) as loc:
        idem_key = f"loc-open-{uuid.uuid4()}"
        selected = await loc.get(
            "/v1/routes", params={"capability": "test:job", "offering": "default"}
        )
        selected.raise_for_status()
        selected_route = selected.json()
        open_body = {
            "capability": "test:job",
            "offering": "default",
            "transport": "unary",
            "estimated_units": 10_250,
            "max_total_units": 10_250,
            "route_binding": selected_route["route_binding"],
        }
        first = await loc.post("/v1/jobs", headers={"Idempotency-Key": idem_key}, json=open_body)
        first.raise_for_status()
        replay = await loc.post("/v1/jobs", headers={"Idempotency-Key": idem_key}, json=open_body)
        replay.raise_for_status()
        if replay.json() != first.json():
            raise AssertionError("LOC job-open replay did not return the durable result")
        if first.json()["route_snapshot"] != selected_route["route_snapshot"]:
            raise AssertionError("LOC job open did not preserve the selected route snapshot")
        reuse = await loc.post(
            "/v1/jobs",
            headers={"Idempotency-Key": idem_key},
            json={**open_body, "max_total_units": 10_251},
        )
        if reuse.status_code != 409:
            raise AssertionError(f"LOC request-id reuse returned {reuse.status_code}, want 409")
        stale_binding = {
            **selected_route["route_binding"],
            "route_fingerprint": "0" * 64,
        }
        stale = await loc.post(
            "/v1/jobs",
            headers={"Idempotency-Key": f"loc-open-stale-{uuid.uuid4()}"},
            json={**open_body, "route_binding": stale_binding},
        )
        if (
            stale.status_code != 409
            or stale.json().get("error", {}).get("code") != "route_binding_mismatch"
        ):
            raise AssertionError(f"LOC stale job route binding was not rejected: {stale.text}")
        job = first.json()
        cases.append(
            {
                "case": "loc_job_open_idempotency",
                "status": "passed",
                "request_id": job["request_id"],
                "job_id": job["job_id"],
            }
        )

        baseline_calls = _BackendHandler.job_calls
        broker_url = str(job["broker_url"]).rstrip("/")
        broker_body = b'{"prompt":"withhold settlement"}'
        async with httpx.AsyncClient(timeout=10) as broker:
            broker_first = await broker.post(
                f"{broker_url}/v1/job",
                headers=_broker_headers(job),
                content=broker_body,
            )
            broker_first.raise_for_status()

            # Payment accounting may finish after a unary response. Wait on
            # the request-id lookup before replaying so this case tests a
            # completed replay rather than the separately valid job_in_flight
            # response. This is also the recovery handle LOC owns even when a
            # customer withholds the broker job id and settlement.
            exchange: dict[str, Any] = {}
            for _ in range(40):
                exchange_response = await broker.get(
                    f"{broker_url}/v1/exchange/{job['request_id']}"
                )
                exchange = exchange_response.json()
                if exchange.get("outcome") == "SETTLED":
                    break
                if exchange.get("outcome") not in {"IN_FLIGHT", "ACCOUNTING_PENDING"}:
                    raise AssertionError(f"broker exchange did not settle: {exchange!r}")
                await asyncio.sleep(0.25)
            else:
                raise AssertionError(f"broker exchange remained pending: {exchange!r}")

            broker_replay = await broker.post(
                f"{broker_url}/v1/job",
                headers=_broker_headers(job),
                content=broker_body,
            )
            broker_replay.raise_for_status()
            if _BackendHandler.job_calls != baseline_calls + 1:
                raise AssertionError("broker replay executed the backend more than once")
            for header in ("Livepeer-Job-Id", "Livepeer-Settlement", "Livepeer-Work-Units"):
                if broker_replay.headers.get(header) != broker_first.headers.get(header):
                    raise AssertionError(f"broker replay changed {header}")
            broker_reuse = await broker.post(
                f"{broker_url}/v1/job",
                headers=_broker_headers(job),
                content=b'{"prompt":"different content"}',
            )
            if broker_reuse.status_code != 400:
                raise AssertionError(
                    f"broker request-id reuse returned {broker_reuse.status_code}, want 400"
                )
            if _BackendHandler.job_calls != baseline_calls + 1:
                raise AssertionError("broker request-id reuse executed the backend")
        cases.append(
            {
                "case": "broker_job_idempotency",
                "status": "passed",
                "request_id": job["request_id"],
                "broker_job_id": exchange["job_id"],
            }
        )

        deadline = time.monotonic() + 10
        status: dict[str, Any] = {}
        while time.monotonic() < deadline:
            status_response = await loc.get(f"/v1/jobs/{job['job_id']}")
            status_response.raise_for_status()
            status = status_response.json()
            if status.get("accounting_outcome") == "broker_settled":
                break
            await asyncio.sleep(0.25)
        else:
            raise AssertionError(f"LOC did not recover withheld settlement: {status!r}")
        if status.get("actual_units") != 1 or status.get("broker_exchange_outcome") != "SETTLED":
            raise AssertionError(f"LOC recovered the wrong broker outcome: {status!r}")
        cases.append(
            {
                "case": "request_id_settlement_recovery",
                "status": "passed",
                "request_id": job["request_id"],
                "accounting_outcome": status["accounting_outcome"],
            }
        )

        cross_key = f"loc-cross-{uuid.uuid4()}"
        cross_open = await loc.post(
            "/v1/jobs", headers={"Idempotency-Key": cross_key}, json=open_body
        )
        cross_open.raise_for_status()
        cross_job = cross_open.json()
        encoded = exchange["settlement"]
        settlement = json.loads(__import__("base64").b64decode(encoded, validate=True))
        cross_settle = await loc.post(
            cross_job["settle_endpoint"],
            json={
                "actual_units": 1,
                "broker_job_id": exchange["job_id"],
                "work_unit": "tokens",
                "settlement": settlement,
            },
        )
        if cross_settle.status_code != 409:
            raise AssertionError(
                f"cross-request settlement returned {cross_settle.status_code}, want 409"
            )
        cross_status = await loc.get(f"/v1/jobs/{cross_job['job_id']}")
        cross_status.raise_for_status()
        if cross_status.json().get("accounting_outcome") != "unresolved":
            raise AssertionError("cross-request evidence changed financial state")
        tampered = json.loads(json.dumps(settlement))
        tampered["payload"]["request_id"] = cross_job["request_id"]
        tampered_settle = await loc.post(
            cross_job["settle_endpoint"],
            json={
                "actual_units": 1,
                "broker_job_id": exchange["job_id"],
                "work_unit": "tokens",
                "settlement": tampered,
            },
        )
        if tampered_settle.status_code != 409:
            raise AssertionError(
                f"tampered signed settlement returned {tampered_settle.status_code}, want 409"
            )
        tampered_status = await loc.get(f"/v1/jobs/{cross_job['job_id']}")
        tampered_status.raise_for_status()
        if tampered_status.json().get("accounting_outcome") != "unresolved":
            raise AssertionError("tampered settlement changed financial state")
        cases.append(
            {
                "case": "cross_request_settlement_rejected",
                "status": "passed",
                "request_id": cross_job["request_id"],
            }
        )
        cases.append(
            {
                "case": "tampered_signed_settlement_rejected",
                "status": "passed",
                "request_id": cross_job["request_id"],
            }
        )

        # Mint but deliberately never present the envelope to the broker.
        # LOC must turn broker silence into a directly retrieved, verified
        # audit record without changing the job's accounting state.
        absent_open = await loc.post(
            "/v1/jobs",
            headers={"Idempotency-Key": f"loc-not-admitted-{uuid.uuid4()}"},
            json=open_body,
        )
        absent_open.raise_for_status()
        absent_job = absent_open.json()
        deadline = time.monotonic() + 10
        absent_status: dict[str, Any] = {}
        while time.monotonic() < deadline:
            response = await loc.get(f"/v1/jobs/{absent_job['job_id']}")
            response.raise_for_status()
            absent_status = response.json()
            if absent_status.get("accounting_outcome") == "non_admission_audit":
                break
            await asyncio.sleep(0.25)
        else:
            raise AssertionError(
                f"LOC did not retain verified non-admission evidence: {absent_status!r}"
            )
        if (
            absent_status.get("state") != "open"
            or absent_status.get("billed_value_wei") is not None
            or absent_status.get("broker_exchange_outcome") != "NOT_ADMITTED"
        ):
            raise AssertionError(f"non-admission changed accounting state: {absent_status!r}")
        cases.append(
            {
                "case": "verified_non_admission_audit_only",
                "status": "passed",
                "request_id": absent_job["request_id"],
            }
        )
    return cases


async def _exercise_session_matrix(  # noqa: PLR0912 — one readable protocol matrix
    loc_url: str, api_key: str
) -> list[dict[str, Any]]:
    """Drive paid-session/v1 open, replay, refill, and close boundaries."""

    cases: list[dict[str, Any]] = []
    async with OpenClearinghouseClient(base_url=loc_url, api_key=api_key) as sdk:
        handle = await sdk.open_session(
            capability="test:session",
            offering="default",
            descriptor_schema="test-runtime/v1",
            session_params={"name": "official-sdk"},
            estimated_runway_units=6_000,
            max_total_units=12_000,
            request_id=f"sdk-session-{uuid.uuid4()}",
        )
        runner = SessionRunner(client=sdk, handle=handle)
        broker_session = await runner.start()
        if broker_session.runtime_schema != "test-runtime/v1" or broker_session.grants:
            raise AssertionError(f"official SDK parsed the wrong runtime: {broker_session!r}")
        status = await runner.status()
        if status.get("state") != "active":
            raise AssertionError(f"broker session status is not active: {status!r}")
        closed = await runner.close(actual_units=0)
        if closed.get("actual_units") != 0 or closed.get("outcome") != "OVERFUNDED":
            raise AssertionError(f"official SDK session close is invalid: {closed!r}")
        cases.append(
            {
                "case": "python_sdk_paid_session",
                "status": "passed",
                "session_id": str(handle.session_id),
                "broker_session_id": broker_session.session_id,
            }
        )

    headers = {"X-API-Key": api_key, "Livepeer-Open-Clearinghouse-SDK": "live-matrix"}
    async with httpx.AsyncClient(base_url=loc_url, headers=headers, timeout=10) as loc:
        idem_key = f"loc-session-{uuid.uuid4()}"
        selected = await loc.get(
            "/v1/routes", params={"capability": "test:session", "offering": "default"}
        )
        selected.raise_for_status()
        selected_route = selected.json()
        open_body = {
            "capability": "test:session",
            "offering": "default",
            "descriptor_schema": "test-runtime/v1",
            "session_params": {"name": "idempotency-matrix"},
            "estimated_runway_units": 6_000,
            "max_total_units": 12_000,
            "route_binding": selected_route["route_binding"],
        }
        first = await loc.post(
            "/v1/sessions", headers={"Idempotency-Key": idem_key}, json=open_body
        )
        first.raise_for_status()
        replay = await loc.post(
            "/v1/sessions", headers={"Idempotency-Key": idem_key}, json=open_body
        )
        replay.raise_for_status()
        if replay.json() != first.json():
            raise AssertionError("LOC session-open replay did not return the durable result")
        if first.json()["route_snapshot"] != selected_route["route_snapshot"]:
            raise AssertionError("LOC session open did not preserve the selected route snapshot")
        reuse = await loc.post(
            "/v1/sessions",
            headers={"Idempotency-Key": idem_key},
            json={**open_body, "max_total_units": 12_001},
        )
        if reuse.status_code != 409:
            raise AssertionError(f"LOC session request-id reuse returned {reuse.status_code}")
        stale_binding = {
            **selected_route["route_binding"],
            "route_fingerprint": "0" * 64,
        }
        stale = await loc.post(
            "/v1/sessions",
            headers={"Idempotency-Key": f"loc-session-stale-{uuid.uuid4()}"},
            json={**open_body, "route_binding": stale_binding},
        )
        if (
            stale.status_code != 409
            or stale.json().get("error", {}).get("code") != "route_binding_mismatch"
        ):
            raise AssertionError(f"LOC stale session route binding was not rejected: {stale.text}")
        opened = first.json()

        broker_url = str(opened["broker_url"]).rstrip("/")
        broker_headers = {
            "Content-Type": "application/json",
            "Livepeer-Capability": "test:session",
            "Livepeer-Offering": "default",
            "Livepeer-Payment": str(opened["payment_envelope"]),
            "Livepeer-Protocol": str(opened["protocol"]),
            "Livepeer-Request-Id": str(opened["request_id"]),
        }
        broker_body = json.dumps(
            {
                "gateway_session_id": opened["session_id"],
                "session_params": open_body["session_params"],
            },
            separators=(",", ":"),
        ).encode()
        async with httpx.AsyncClient(timeout=10) as broker:
            broker_first = await broker.post(
                f"{broker_url}/v1/session", headers=broker_headers, content=broker_body
            )
            broker_first.raise_for_status()
            broker_replay = await broker.post(
                f"{broker_url}/v1/session", headers=broker_headers, content=broker_body
            )
            broker_replay.raise_for_status()
            if broker_replay.json() != broker_first.json():
                raise AssertionError("broker session-open replay changed the usable outcome")
            session = broker_first.json()
            runtime = session.get("runtime", {})
            if runtime.get("schema") != "test-runtime/v1" or "grants" in runtime:
                raise AssertionError(f"broker emitted an invalid empty-grants runtime: {runtime!r}")

            refill_key = f"refill-{uuid.uuid4()}"
            refill_body = {"observed_consumed_units": 0}
            refill_first = await loc.post(
                opened["refill_endpoint"],
                headers={"Idempotency-Key": refill_key},
                json=refill_body,
            )
            if refill_first.is_error:
                raise AssertionError(
                    f"LOC session refill returned {refill_first.status_code}: {refill_first.text}"
                )
            refill_replay = await loc.post(
                opened["refill_endpoint"],
                headers={"Idempotency-Key": refill_key},
                json=refill_body,
            )
            refill_replay.raise_for_status()
            if refill_replay.json() != refill_first.json():
                raise AssertionError("LOC refill replay did not return the durable result")
            refill = refill_first.json()
            topup_headers = {
                "Authorization": f"Bearer {session['credential']}",
                "Livepeer-Payment": str(refill["payment_envelope"]),
                "Livepeer-Request-Id": str(refill["request_id"]),
            }
            topup_first = await broker.post(session["control"]["topup_url"], headers=topup_headers)
            topup_first.raise_for_status()
            topup_replay = await broker.post(session["control"]["topup_url"], headers=topup_headers)
            topup_replay.raise_for_status()
            if topup_replay.json() != topup_first.json():
                raise AssertionError("broker top-up replay changed the recorded outcome")

            ended = await broker.post(
                session["control"]["end_url"],
                headers={"Authorization": f"Bearer {session['credential']}"},
                json={"reason": "conformance_complete"},
            )
            ended.raise_for_status()
            encoded = ended.headers.get("Livepeer-Settlement")
            if not encoded:
                raise AssertionError("broker session end omitted its signed settlement")
            settlement = json.loads(__import__("base64").b64decode(encoded, validate=True))
            if settlement.get("payload", {}).get("state") != "closed":
                raise AssertionError("broker signed a non-normative terminal session state")
            closed = await loc.post(
                opened["close_endpoint"],
                json={"actual_units": 0, "settlement": settlement},
            )
            closed.raise_for_status()
            if closed.json().get("outcome") != "OVERFUNDED":
                raise AssertionError(f"LOC session close is invalid: {closed.json()!r}")

        cases.extend(
            [
                {
                    "case": "session_open_idempotency",
                    "status": "passed",
                    "session_id": opened["session_id"],
                    "broker_session_id": session["session_id"],
                },
                {
                    "case": "session_refill_idempotency",
                    "status": "passed",
                    "session_id": opened["session_id"],
                    "refill_seq": refill["refill_seq"],
                },
                {
                    "case": "signed_session_terminal_close",
                    "status": "passed",
                    "session_id": opened["session_id"],
                },
            ]
        )
    return cases


def _rotate_payee_recipient(payee_socket: Path, payment_envelope: str) -> str:
    """Rotate the exact payer/payee tuple carried by a live payment."""

    payment = types_pb2.Payment()
    payment.ParseFromString(base64.b64decode(payment_envelope, validate=True))
    channel = grpc.insecure_channel(f"unix://{payee_socket}")
    try:
        response = payee_admin_pb2_grpc.PayeeAdminStub(channel).ResetSession(  # type: ignore[no-untyped-call]
            payee_admin_pb2.ResetSessionRequest(
                sender=payment.sender,
                recipient=payment.ticket_params.recipient,
                capability="test:session",
                offering="default",
            ),
            metadata=(("authorization", f"Bearer {_PAYEE_ADMIN_TOKEN}"),),
            timeout=5,
        )
    finally:
        channel.close()
    if not response.reset or not response.old_work_id:
        raise AssertionError(f"payee did not rotate its recipient: {response!r}")
    return str(response.old_work_id)


async def _exercise_session_rotation(
    loc_url: str, api_key: str, payee_socket: Path
) -> dict[str, Any]:
    """Prove stale-refill rejection and an exactly-once replacement rebind."""

    headers = {"X-API-Key": api_key, "Livepeer-Open-Clearinghouse-SDK": "live-matrix"}
    async with (
        httpx.AsyncClient(base_url=loc_url, headers=headers, timeout=10) as loc,
        httpx.AsyncClient(timeout=10) as broker,
    ):
        opened_response = await loc.post(
            "/v1/sessions",
            headers={"Idempotency-Key": f"rotation-open-{uuid.uuid4()}"},
            json={
                "capability": "test:session",
                "offering": "default",
                "descriptor_schema": "test-runtime/v1",
                "session_params": {"name": "rotation-matrix"},
                "estimated_runway_units": 6_000,
                "max_total_units": 18_000,
            },
        )
        opened_response.raise_for_status()
        opened = opened_response.json()
        broker_url = str(opened["broker_url"]).rstrip("/")
        broker_open = await broker.post(
            f"{broker_url}/v1/session",
            headers={
                "Content-Type": "application/json",
                "Livepeer-Capability": "test:session",
                "Livepeer-Offering": "default",
                "Livepeer-Payment": str(opened["payment_envelope"]),
                "Livepeer-Protocol": str(opened["protocol"]),
                "Livepeer-Request-Id": str(opened["request_id"]),
            },
            content=json.dumps(
                {
                    "gateway_session_id": opened["session_id"],
                    "session_params": {"name": "rotation-matrix"},
                },
                separators=(",", ":"),
            ).encode(),
        )
        broker_open.raise_for_status()
        session = broker_open.json()

        stale_response = await loc.post(
            opened["refill_endpoint"],
            headers={"Idempotency-Key": f"rotation-stale-{uuid.uuid4()}"},
            json={"observed_consumed_units": 0},
        )
        stale_response.raise_for_status()
        stale = stale_response.json()
        predecessor = _rotate_payee_recipient(payee_socket, str(stale["payment_envelope"]))
        if predecessor != stale["work_id"] or predecessor != opened["work_id"]:
            raise AssertionError("payee rotated a different payment identity than the LOC session")

        stale_topup = await broker.post(
            session["control"]["topup_url"],
            headers={
                "Authorization": f"Bearer {session['credential']}",
                "Livepeer-Payment": str(stale["payment_envelope"]),
                "Livepeer-Request-Id": str(stale["request_id"]),
            },
        )
        if (
            stale_topup.status_code != 409
            or stale_topup.headers.get("Livepeer-Error") != "recipient_rotated"
        ):
            raise AssertionError(
                "stale refill did not return the recipient_rotated contract: "
                f"{stale_topup.status_code} {stale_topup.text}"
            )

        replacement_response = await loc.post(
            opened["refill_endpoint"],
            headers={"Idempotency-Key": f"rotation-successor-{uuid.uuid4()}"},
            json={
                "observed_consumed_units": 0,
                "rebind_from": predecessor,
                "replaces_request_id": stale["request_id"],
            },
        )
        replacement_response.raise_for_status()
        replacement = replacement_response.json()
        if replacement["work_id"] == predecessor or replacement["rebind_from"] != predecessor:
            raise AssertionError("LOC did not mint a successor bound to the predecessor")

        rebind_headers = {
            "Authorization": f"Bearer {session['credential']}",
            "Livepeer-Payment": str(replacement["payment_envelope"]),
            "Livepeer-Request-Id": str(replacement["request_id"]),
            "Livepeer-Rebind-From": predecessor,
        }
        rebound = await broker.post(session["control"]["topup_url"], headers=rebind_headers)
        rebound.raise_for_status()
        rebound_replay = await broker.post(session["control"]["topup_url"], headers=rebind_headers)
        rebound_replay.raise_for_status()
        if rebound_replay.json() != rebound.json():
            raise AssertionError("broker rotation replay changed the recorded outcome")

        ended = await broker.post(
            session["control"]["end_url"],
            headers={"Authorization": f"Bearer {session['credential']}"},
            json={"reason": "rotation_conformance_complete"},
        )
        ended.raise_for_status()
        encoded = ended.headers.get("Livepeer-Settlement")
        if not encoded:
            raise AssertionError("rotated session end omitted its signed settlement")
        settlement = json.loads(base64.b64decode(encoded, validate=True))
        payload = settlement.get("payload", {})
        if (
            payload.get("rotation_generation") != 1
            or payload.get("predecessor_work_id") != predecessor
            or payload.get("work_id") != replacement["work_id"]
        ):
            raise AssertionError(f"rotated settlement lost its identity chain: {payload!r}")
        closed = await loc.post(
            opened["close_endpoint"],
            json={"actual_units": 0, "settlement": settlement},
        )
        closed.raise_for_status()
        if closed.json().get("outcome") != "OVERFUNDED":
            raise AssertionError(f"rotated LOC close is invalid: {closed.json()!r}")

        return {
            "case": "session_recipient_rotation_rebind",
            "status": "passed",
            "session_id": opened["session_id"],
            "predecessor_work_id": predecessor,
            "work_id": replacement["work_id"],
            "rotation_generation": payload["rotation_generation"],
        }


async def _exercise_proactive_nonce_boundary_rotation(loc_url: str, api_key: str) -> dict[str, Any]:
    """Cross the real 600-ticket boundary through LOC and broker APIs."""

    headers = {"X-API-Key": api_key, "Livepeer-Open-Clearinghouse-SDK": "live-matrix"}
    estimated_units = 6_000
    total_payments = 601
    async with (
        httpx.AsyncClient(base_url=loc_url, headers=headers, timeout=20) as loc,
        httpx.AsyncClient(timeout=20) as broker,
    ):
        opened_response = await loc.post(
            "/v1/sessions",
            headers={"Idempotency-Key": f"proactive-open-{uuid.uuid4()}"},
            json={
                "capability": "test:session",
                "offering": "default",
                "descriptor_schema": "test-runtime/v1",
                "session_params": {"name": "proactive-nonce-boundary"},
                "estimated_runway_units": estimated_units,
                "max_total_units": estimated_units * total_payments,
            },
        )
        opened_response.raise_for_status()
        opened = opened_response.json()
        predecessor = str(opened["work_id"])
        broker_url = str(opened["broker_url"]).rstrip("/")
        broker_open = await broker.post(
            f"{broker_url}/v1/session",
            headers={
                "Content-Type": "application/json",
                "Livepeer-Capability": "test:session",
                "Livepeer-Offering": "default",
                "Livepeer-Payment": str(opened["payment_envelope"]),
                "Livepeer-Protocol": str(opened["protocol"]),
                "Livepeer-Request-Id": str(opened["request_id"]),
            },
            content=json.dumps(
                {
                    "gateway_session_id": opened["session_id"],
                    "session_params": {"name": "proactive-nonce-boundary"},
                },
                separators=(",", ":"),
            ).encode(),
        )
        broker_open.raise_for_status()
        session = broker_open.json()

        # Payments may contain more than one ticket, so the authoritative
        # boundary is the payer's declared predecessor rather than a guessed
        # payment ordinal. This funded range must cross 600 accepted tickets.
        boundary: dict[str, Any] | None = None
        boundary_payment = 0
        for payment_number in range(2, total_payments + 1):
            refill_key = f"proactive-{payment_number}-{uuid.uuid4()}"
            refill_response = await loc.post(
                opened["refill_endpoint"],
                headers={"Idempotency-Key": refill_key},
                json={"observed_consumed_units": 0},
            )
            refill_response.raise_for_status()
            refill = refill_response.json()
            if refill.get("rebind_from") is not None:
                if refill["work_id"] == predecessor or refill.get("rebind_from") != predecessor:
                    raise AssertionError(f"LOC lost the proactive rollover pair: {refill!r}")
                replay = await loc.post(
                    opened["refill_endpoint"],
                    headers={"Idempotency-Key": refill_key},
                    json={"observed_consumed_units": 0},
                )
                replay.raise_for_status()
                if replay.json() != refill:
                    raise AssertionError(
                        "proactive LOC rollover replay changed its durable response"
                    )
                boundary = refill
                boundary_payment = payment_number
                break
            if refill["work_id"] != predecessor:
                raise AssertionError(f"payer silently changed work ID at payment {payment_number}")
            topup = await broker.post(
                session["control"]["topup_url"],
                headers={
                    "Authorization": f"Bearer {session['credential']}",
                    "Livepeer-Payment": str(refill["payment_envelope"]),
                    "Livepeer-Request-Id": str(refill["request_id"]),
                },
            )
            topup.raise_for_status()

        if boundary is None:
            raise AssertionError(f"payer did not rotate within {total_payments} funded payments")

        rebind_headers = {
            "Authorization": f"Bearer {session['credential']}",
            "Livepeer-Payment": str(boundary["payment_envelope"]),
            "Livepeer-Request-Id": str(boundary["request_id"]),
            "Livepeer-Rebind-From": predecessor,
        }
        rebound = await broker.post(session["control"]["topup_url"], headers=rebind_headers)
        rebound.raise_for_status()
        rebound_replay = await broker.post(session["control"]["topup_url"], headers=rebind_headers)
        rebound_replay.raise_for_status()
        if rebound_replay.json() != rebound.json():
            raise AssertionError("proactive broker rebind replay changed its durable response")

        ended = await broker.post(
            session["control"]["end_url"],
            headers={"Authorization": f"Bearer {session['credential']}"},
            json={"reason": "proactive_nonce_boundary_complete"},
        )
        ended.raise_for_status()
        encoded = ended.headers.get("Livepeer-Settlement")
        if not encoded:
            raise AssertionError("proactively rotated session omitted terminal settlement")
        settlement = json.loads(base64.b64decode(encoded, validate=True))
        payload = settlement.get("payload", {})
        if (
            payload.get("rotation_generation") != 1
            or payload.get("predecessor_work_id") != predecessor
            or payload.get("work_id") != boundary["work_id"]
        ):
            raise AssertionError(f"proactive settlement lost its identity chain: {payload!r}")
        closed = await loc.post(
            opened["close_endpoint"],
            json={"actual_units": 0, "settlement": settlement},
        )
        closed.raise_for_status()

        return {
            "case": "session_proactive_nonce_boundary_rotation",
            "status": "passed",
            "session_id": opened["session_id"],
            "rollover_payment": boundary_payment,
            "predecessor_work_id": predecessor,
            "work_id": boundary["work_id"],
            "rotation_generation": payload["rotation_generation"],
        }


async def _exercise_transient_debit_failure(  # noqa: PLR0912 — one fault lifecycle
    loc_url: str,
    api_key: str,
    *,
    stop_payee: Callable[[], None],
    restart_payee: Callable[[], None],
) -> dict[str, Any]:
    """Interrupt the payee after admission and prove durable debit recovery."""

    headers = {"X-API-Key": api_key, "Livepeer-Open-Clearinghouse-SDK": "live-matrix"}
    async with (
        httpx.AsyncClient(base_url=loc_url, headers=headers, timeout=10) as loc,
        httpx.AsyncClient(timeout=15) as broker,
    ):
        opened_response = await loc.post(
            "/v1/jobs",
            headers={"Idempotency-Key": f"debit-fault-open-{uuid.uuid4()}"},
            json={
                "capability": "test:job",
                "offering": "default",
                "transport": "unary",
                "estimated_units": 10_250,
                "max_total_units": 10_250,
            },
        )
        opened_response.raise_for_status()
        opened = opened_response.json()
        broker_url = str(opened["broker_url"]).rstrip("/")
        baseline_calls = _BackendHandler.job_calls
        _BackendHandler.blocked_job_entered.clear()
        _BackendHandler.blocked_job_release.clear()
        _BackendHandler.block_next_job = True
        exchange_task = asyncio.create_task(
            broker.post(
                f"{broker_url}/v1/job",
                headers=_broker_headers(opened),
                content=b'{"prompt":"interrupt debit after delivery"}',
            )
        )
        entered = await asyncio.to_thread(_BackendHandler.blocked_job_entered.wait, 5)
        if not entered:
            _BackendHandler.blocked_job_release.set()
            raise AssertionError("fault-injection backend was not entered")
        try:
            stop_payee()
        finally:
            _BackendHandler.blocked_job_release.set()
        broker_response = await exchange_task
        broker_response.raise_for_status()
        if encoded_pending := broker_response.headers.get("Livepeer-Settlement"):
            pending_claim = json.loads(base64.b64decode(encoded_pending, validate=True))
            raise AssertionError(
                "ACCOUNTING_PENDING response carried a terminal settlement: "
                f"{pending_claim.get('payload', {}).get('outcome')!r}"
            )
        if _BackendHandler.job_calls != baseline_calls + 1:
            raise AssertionError("debit fault executed backend work more than once")

        pending = _job_exchange(broker_url, str(opened["request_id"]))
        if pending.get("outcome") != "ACCOUNTING_PENDING":
            raise AssertionError(f"failed debit was not durably pending: {pending!r}")
        restart_payee()

        exchange: dict[str, Any] = {}
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            exchange = _job_exchange(broker_url, str(opened["request_id"]))
            if exchange.get("outcome") == "SETTLED":
                break
            if exchange.get("outcome") != "ACCOUNTING_PENDING":
                raise AssertionError(f"debit retry entered an invalid outcome: {exchange!r}")
            await asyncio.sleep(0.5)
        else:
            raise AssertionError(f"broker did not recover the failed debit: {exchange!r}")
        if not exchange.get("settlement"):
            raise AssertionError("recovered debit omitted signed settlement evidence")

        loc_status: dict[str, Any] = {}
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            status_response = await loc.get(f"/v1/jobs/{opened['job_id']}")
            status_response.raise_for_status()
            loc_status = status_response.json()
            if loc_status.get("accounting_outcome") == "broker_settled":
                break
            await asyncio.sleep(0.25)
        else:
            raise AssertionError(f"LOC did not recover the retried debit: {loc_status!r}")
        if loc_status.get("actual_units") != 1 or _BackendHandler.job_calls != baseline_calls + 1:
            raise AssertionError("debit recovery changed usage or repeated backend work")

        return {
            "case": "transient_debit_failure_recovers",
            "status": "passed",
            "request_id": opened["request_id"],
            "broker_job_id": exchange["job_id"],
            "pending_attempts": pending.get("debit_attempts"),
        }


def run(repo: Path, modules_repo: Path, artifacts: Path) -> dict[str, Any]:
    for command in ("docker", "git", "go", "uv"):
        if shutil.which(command) is None:
            raise RuntimeError(f"required command is unavailable: {command}")

    repo = repo.resolve()
    modules_repo = modules_repo.resolve()
    artifacts = artifacts.resolve()
    artifacts.mkdir(parents=True, exist_ok=True)
    processes: list[_RunningProcess] = []
    container_name = f"loc-live-pg-{uuid.uuid4().hex[:10]}"

    with tempfile.TemporaryDirectory(prefix="loc-live-stack-") as raw_runtime, ExitStack() as stack:
        runtime = Path(raw_runtime)
        bin_dir = runtime / "bin"
        bin_dir.mkdir()
        payer_bin = bin_dir / "livepeer-payment-daemon"
        registry_bin = bin_dir / "livepeer-service-registry-daemon"
        broker_bin = bin_dir / "livepeer-capability-broker"

        _build_go_binary(
            modules_repo / "payment-daemon",
            "./cmd/livepeer-payment-daemon",
            payer_bin,
            artifacts / "build-payment-daemon.log",
        )
        _build_go_binary(
            modules_repo / "service-registry-daemon",
            "./cmd/livepeer-service-registry-daemon",
            registry_bin,
            artifacts / "build-service-registry-daemon.log",
        )
        _build_go_binary(
            modules_repo / "capability-broker",
            "./cmd/livepeer-capability-broker",
            broker_bin,
            artifacts / "build-capability-broker.log",
        )

        backend = ThreadingHTTPServer(("127.0.0.1", 0), _BackendHandler)
        backend_thread = __import__("threading").Thread(target=backend.serve_forever, daemon=True)
        backend_thread.start()
        stack.callback(backend_thread.join, 5)
        stack.callback(backend.server_close)
        stack.callback(backend.shutdown)
        backend_url = f"http://127.0.0.1:{backend.server_port}"

        payer_socket = runtime / "payer.sock"
        payee_socket = runtime / "payee.sock"
        payer_process = _start_process(
            "payer-daemon",
            [
                str(payer_bin),
                "--mode=sender",
                f"--socket={payer_socket}",
                f"--db={runtime / 'payer.db'}",
            ],
            artifacts,
            processes,
            cwd=modules_repo / "payment-daemon",
        )
        stack.callback(payer_process.stop)
        payee_command = [
            str(payer_bin),
            "--mode=receiver",
            f"--socket={payee_socket}",
            f"--db={runtime / 'payee.db'}",
            f"--orch-address={COLD_KEY.public_key.to_address()}",
            f"--payee-admin-token={_PAYEE_ADMIN_TOKEN}",
        ]
        payee_process = _start_process(
            "payee-daemon",
            payee_command,
            artifacts,
            processes,
            cwd=modules_repo / "payment-daemon",
        )
        stack.callback(payee_process.stop)
        _wait_for(
            "payment daemon sockets",
            lambda: payer_socket.exists() and payee_socket.exists(),
            processes,
        )

        settlement_key_path = runtime / "settlement-key.hex"
        settlement_key_path.write_text(SETTLEMENT_KEY.to_bytes().hex() + "\n")
        sealing_key_path = runtime / "sealing-key.hex"
        sealing_key_path.write_text((b"\x03" * 32).hex() + "\n")
        broker_paid_port = _free_port()
        broker_metrics_port = _free_port()
        broker_config = runtime / "broker.yaml"
        _write_broker_config(
            broker_config,
            paid_port=broker_paid_port,
            metrics_port=broker_metrics_port,
            payee_socket=payee_socket,
            backend_url=backend_url,
            settlement_key_path=settlement_key_path,
            session_store_path=runtime / "broker-state.db",
            sealing_key_path=sealing_key_path,
        )
        broker_process = _start_process(
            "capability-broker",
            [str(broker_bin), f"--config={broker_config}"],
            artifacts,
            processes,
            cwd=modules_repo / "capability-broker",
        )
        stack.callback(broker_process.stop)
        broker_url = f"http://127.0.0.1:{broker_paid_port}"
        _wait_for(
            "broker registry health",
            lambda: any(
                cap.get("id") == "test:job" and cap.get("status") == "ready"
                for cap in _http_json(f"{broker_url}/registry/health").get("capabilities", [])
            ),
            processes,
        )

        manifest_path = runtime / "manifest.json"
        manifest_path.write_text(json.dumps(_signed_manifest(broker_url), indent=2) + "\n")
        manifest_handler = partial(_QuietHandler, directory=str(runtime))
        manifest_server = ThreadingHTTPServer(("127.0.0.1", 0), manifest_handler)
        manifest_thread = __import__("threading").Thread(
            target=manifest_server.serve_forever, daemon=True
        )
        manifest_thread.start()
        stack.callback(manifest_thread.join, 5)
        stack.callback(manifest_server.server_close)
        stack.callback(manifest_server.shutdown)
        seed_path = runtime / "chain-seed.yaml"
        seed_path.write_text(
            "seed:\n"
            f'  - eth_address: "{COLD_KEY.public_key.to_address()}"\n'
            f'    service_uri: "http://127.0.0.1:{manifest_server.server_port}/manifest.json"\n'
        )
        registry_socket = runtime / "registry.sock"
        registry_process = _start_process(
            "service-registry-daemon",
            [
                str(registry_bin),
                "--mode=resolver",
                "--dev",
                f"--chain-seed={seed_path}",
                f"--socket={registry_socket}",
                "--metrics-listen=",
            ],
            artifacts,
            processes,
            cwd=modules_repo / "service-registry-daemon",
        )
        stack.callback(registry_process.stop)
        channel = grpc.insecure_channel(f"unix://{registry_socket}")
        stack.callback(channel.close)
        _wait_for(
            "registry gRPC socket",
            lambda: _grpc_ready(channel),
            [registry_process],
        )
        _prime_registry(channel)
        route = asyncio.run(_select_route(registry_socket))

        postgres_port = _free_port()
        docker = shutil.which("docker")
        uv = shutil.which("uv")
        assert docker is not None
        assert uv is not None
        subprocess.run(  # noqa: S603 — fixed Docker harness lifecycle
            [
                docker,
                "run",
                "--rm",
                "--detach",
                "--name",
                container_name,
                "--publish",
                f"127.0.0.1:{postgres_port}:5432",
                "--env",
                "POSTGRES_USER=loc",
                "--env",
                "POSTGRES_PASSWORD=loc",
                "--env",
                "POSTGRES_DB=loc",
                "postgres:16-alpine",
            ],
            cwd=repo,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        stack.callback(_remove_container, container_name, repo)
        database_url = f"postgresql+asyncpg://loc:loc@127.0.0.1:{postgres_port}/loc"
        _wait_for(
            "Postgres",
            lambda: asyncio.run(_postgres_ready(database_url)),
            processes,
            timeout=60,
        )
        app_env = os.environ.copy()
        app_env.update(
            {
                "DATABASE_URL": database_url,
                "PAYMENT_DAEMON_MODE": "grpc",
                "PAYMENT_DAEMON_SOCKET": str(payer_socket),
                "REGISTRY_DAEMON_MODE": "grpc",
                "REGISTRY_DAEMON_SOCKET": str(registry_socket),
                "REGISTRY_CACHE_TTL_SECONDS": "0",
                "API_KEY_HASH_PEPPER": "live-matrix-pepper",
                "EMAIL_PROVIDER": "null",
                "AUTO_REPLENISH_CHECK_INTERVAL_SECONDS": "0",
                "JOB_RECONCILIATION_INTERVAL_SECONDS": "1",
                "JOB_CONSERVATIVE_CHARGE_AFTER_SECONDS": "0",
                "SESSION_RECONCILIATION_INTERVAL_SECONDS": "0",
                "TELEMETRY_RAW_RETENTION_DAYS": "0",
            }
        )
        with (artifacts / "alembic.log").open("w") as migration_log:
            subprocess.run(  # noqa: S603 — fixed migration command
                [uv, "run", "alembic", "upgrade", "head"],
                cwd=repo,
                env=app_env,
                check=True,
                stdout=migration_log,
                stderr=subprocess.STDOUT,
                text=True,
            )
        api_key = asyncio.run(_seed_customer(database_url, pepper=app_env["API_KEY_HASH_PEPPER"]))
        loc_port = _free_port()
        loc_process = _start_process(
            "livepeer-open-clearinghouse",
            [
                uv,
                "run",
                "uvicorn",
                "livepeer_open_clearinghouse.main:app",
                "--host=127.0.0.1",
                f"--port={loc_port}",
            ],
            artifacts,
            processes,
            cwd=repo,
            env=app_env,
        )
        stack.callback(loc_process.stop)
        loc_url = f"http://127.0.0.1:{loc_port}"
        _wait_for(
            "LOC health",
            lambda: _http_json(f"{loc_url}/health").get("status") == "ok",
            processes,
            timeout=60,
        )
        cases = asyncio.run(_exercise_job_matrix(loc_url, api_key))

        recovery_case = next(
            case for case in cases if case["case"] == "request_id_settlement_recovery"
        )
        recovery_request_id = str(recovery_case["request_id"])
        before_restart = _job_exchange(broker_url, recovery_request_id)
        if before_restart.get("outcome") != "SETTLED" or not before_restart.get("settlement"):
            raise AssertionError(
                f"broker held no terminal evidence before restart: {before_restart!r}"
            )
        broker_process.stop()
        processes.remove(broker_process)
        restarted_broker = _start_process(
            "capability-broker-restarted",
            [str(broker_bin), f"--config={broker_config}"],
            artifacts,
            processes,
            cwd=modules_repo / "capability-broker",
        )
        stack.callback(restarted_broker.stop)
        _wait_for(
            "restarted broker registry health",
            lambda: any(
                cap.get("id") == "test:job" and cap.get("status") == "ready"
                for cap in _http_json(f"{broker_url}/registry/health").get("capabilities", [])
            ),
            processes,
        )
        after_restart = _job_exchange(broker_url, recovery_request_id)
        if after_restart != before_restart:
            raise AssertionError("broker restart changed retained request-ID settlement evidence")
        cases.append(
            {
                "case": "request_id_settlement_survives_broker_restart",
                "status": "passed",
                "request_id": recovery_request_id,
                "broker_job_id": after_restart["job_id"],
            }
        )
        cases.extend(asyncio.run(_exercise_session_matrix(loc_url, api_key)))
        cases.append(asyncio.run(_exercise_session_rotation(loc_url, api_key, payee_socket)))
        cases.append(asyncio.run(_exercise_proactive_nonce_boundary_rotation(loc_url, api_key)))

        def stop_payee_for_fault() -> None:
            payee_process.stop()
            processes.remove(payee_process)

        def restart_payee_after_fault() -> None:
            nonlocal payee_process
            payee_process = _start_process(
                "payee-daemon-restarted",
                payee_command,
                artifacts,
                processes,
                cwd=modules_repo / "payment-daemon",
            )
            stack.callback(payee_process.stop)
            payee_channel = grpc.insecure_channel(f"unix://{payee_socket}")
            try:
                _wait_for(
                    "restarted payee gRPC socket",
                    lambda: _grpc_ready(payee_channel),
                    [payee_process],
                )
            finally:
                payee_channel.close()

        cases.append(
            asyncio.run(
                _exercise_transient_debit_failure(
                    loc_url,
                    api_key,
                    stop_payee=stop_payee_for_fault,
                    restart_payee=restart_payee_after_fault,
                )
            )
        )

        result = {
            "status": "ok",
            "loc": {**_git_state(repo), "url": loc_url},
            "modules": _git_state(modules_repo),
            "processes": [process.name for process in processes],
            "postgres": "postgres:16-alpine",
            "route": route,
            "cases": cases,
        }
        (artifacts / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        return result


def _grpc_ready(channel: grpc.Channel) -> bool:
    ready = grpc.channel_ready_future(channel)
    try:
        ready.result(timeout=0.2)
    except grpc.FutureTimeoutError:
        return False
    finally:
        ready.cancel()
    return True


def _prime_registry(channel: grpc.Channel) -> None:
    import importlib

    resolver_pb2 = importlib.import_module("livepeer.registry.v1.resolver_pb2")
    resolver_pb2_grpc = importlib.import_module("livepeer.registry.v1.resolver_pb2_grpc")
    stub = resolver_pb2_grpc.ResolverStub(channel)
    result = stub.ResolveByAddress(
        resolver_pb2.ResolveByAddressRequest(
            eth_address=COLD_KEY.public_key.to_address(), force_refresh=True
        ),
        timeout=5,
    )
    if not result.nodes:
        raise AssertionError("registry resolved the seeded manifest to zero nodes")


def _remove_container(name: str, cwd: Path) -> None:
    docker = shutil.which("docker")
    if docker is None:
        return
    subprocess.run(  # noqa: S603 — exact harness-owned container name
        [docker, "rm", "--force", name],
        cwd=cwd,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    repo = Path(__file__).resolve().parents[2]
    parser.add_argument("--repo", type=Path, default=repo)
    parser.add_argument(
        "--modules-repo",
        type=Path,
        default=repo.parent / "livepeer-network-modules",
    )
    parser.add_argument(
        "--artifacts",
        type=Path,
        default=Path(".artifacts/live-conformance/stack"),
    )
    args = parser.parse_args()
    started = time.monotonic()
    try:
        result = run(args.repo, args.modules_repo, args.artifacts)
    except Exception as exc:
        args.artifacts.mkdir(parents=True, exist_ok=True)
        failure = {"status": "failed", "error": str(exc), "traceback": traceback.format_exc()}
        (args.artifacts / "failure.json").write_text(json.dumps(failure, indent=2) + "\n")
        raise
    result["elapsed_seconds"] = round(time.monotonic() - started, 3)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
