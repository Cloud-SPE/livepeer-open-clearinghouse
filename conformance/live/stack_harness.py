"""Boot the hermetic real-process LOC + Modules v2 stack.

The chain address-to-manifest lookup and the workload backend are the only
fakes. Registry, payer, payee, broker, Postgres, and LOC all run as their real
processes and communicate over their production HTTP, PostgreSQL, and UDS
boundaries.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import socket
import subprocess
import tempfile
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
from typing import IO, Any

import asyncpg
import grpc
from registry_seed_probe import (  # type: ignore[import-not-found]
    COLD_KEY,
    SETTLEMENT_KEY,
    _signed_manifest,
)

from livepeer_open_clearinghouse.providers.registry_daemon import GrpcRegistryClient


class _BackendHandler(BaseHTTPRequestHandler):
    def log_message(self, _format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        if self.path != "/healthz":
            self.send_error(404)
            return
        self._json({"ready": True})

    def do_POST(self) -> None:
        if self.path != "/count":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        if length:
            self.rfile.read(length)
        self._json({"bark_count": 1})

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
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    path.write_text(
        f"""identity:
  orch_eth_address: "{COLD_KEY.public_key.to_address()}"
  label: "loc-live-harness"
  settlement_key_file: "{settlement_key_path}"
  settlement_key_not_before: "{(now - timedelta(hours=1)).isoformat().replace("+00:00", "Z")}"
  settlement_key_expires_at: "{(now + timedelta(hours=23)).isoformat().replace("+00:00", "Z")}"
listen:
  paid: "127.0.0.1:{paid_port}"
  metrics: "127.0.0.1:{metrics_port}"
payment_daemon:
  socket: "{payee_socket}"
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
    price:
      amount_wei: "100"
      per_units: 1000
    backend:
      transport: "http"
      url: "{backend_url}/count"
      auth: "none"
"""
    )


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
        payee_process = _start_process(
            "payee-daemon",
            [
                str(payer_bin),
                "--mode=receiver",
                f"--socket={payee_socket}",
                f"--db={runtime / 'payee.db'}",
                f"--orch-address={COLD_KEY.public_key.to_address()}",
            ],
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
                "EMAIL_PROVIDER": "null",
                "AUTO_REPLENISH_CHECK_INTERVAL_SECONDS": "0",
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

        result = {
            "status": "ok",
            "loc": {**_git_state(repo), "url": loc_url},
            "modules": _git_state(modules_repo),
            "processes": [process.name for process in processes],
            "postgres": "postgres:16-alpine",
            "route": route,
        }
        (artifacts / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        return result


def _grpc_ready(channel: grpc.Channel) -> bool:
    try:
        grpc.channel_ready_future(channel).result(timeout=0.2)
    except grpc.FutureTimeoutError:
        return False
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
