"""Prove the real registry's hermetic signed-manifest path for LOC.

This probe deliberately uses the daemon process and its Unix-socket gRPC
surface. Only the on-chain address-to-serviceURI lookup is replaced by the
registry's ``--chain-seed`` dev provider.
"""

from __future__ import annotations

import argparse
import importlib
import json
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import UTC, datetime, timedelta
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import grpc
import rfc8785
from eth_hash.auto import keccak
from eth_keys.datatypes import PrivateKey

from livepeer_open_clearinghouse import _gen  # noqa: F401

resolver_pb2 = importlib.import_module("livepeer.registry.v1.resolver_pb2")
resolver_pb2_grpc = importlib.import_module("livepeer.registry.v1.resolver_pb2_grpc")

COLD_KEY = PrivateKey(b"\x02" * 32)
SETTLEMENT_KEY = PrivateKey(b"\x01" * 32)


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, _format: str, *args: object) -> None:
        return


def _rfc3339(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _sign_personal(payload: bytes, key: PrivateKey) -> str:
    prefix = f"\x19Ethereum Signed Message:\n{len(payload)}".encode()
    signature = bytearray(key.sign_msg_hash(keccak(prefix + payload)).to_bytes())
    signature[64] += 27
    return "0x" + signature.hex()


def _signed_manifest(worker_url: str) -> dict[str, Any]:
    now = datetime.now(UTC).replace(microsecond=0)
    manifest: dict[str, Any] = {
        "spec_version": "1.0.0",
        "publication_seq": 7,
        "issued_at": _rfc3339(now),
        "expires_at": _rfc3339(now + timedelta(hours=24)),
        "orch": {"eth_address": COLD_KEY.public_key.to_address()},
        "settlement_keys": [
            {
                "public_key": "0x04" + SETTLEMENT_KEY.public_key.to_bytes().hex(),
                "not_before": _rfc3339(now - timedelta(hours=1)),
                "expires_at": _rfc3339(now + timedelta(hours=23)),
            }
        ],
        "capabilities": [
            {
                "capability_id": "test:job",
                "offering_id": "default",
                "protocol": "paid-job/v1",
                "job": {"transports": ["unary", "stream"]},
                "work_unit": {"name": "tokens"},
                "price_per_unit_wei": "100",
                "per_units": 1000,
                "worker_url": worker_url,
            },
            {
                "capability_id": "test:session",
                "offering_id": "default",
                "protocol": "paid-session/v1",
                "session": {
                    "descriptor_schema": "test-runtime/v1",
                    "attachment": "external",
                    "metering": "runner-reported",
                    "refill": "extensible",
                },
                "work_unit": {"name": "seconds"},
                "price_per_unit_wei": "200",
                "worker_url": worker_url,
            },
        ],
    }
    return {
        "manifest": manifest,
        "signature": {
            "algorithm": "secp256k1",
            "canonicalization": "JCS",
            "value": _sign_personal(rfc8785.dumps(manifest), COLD_KEY),
        },
    }


def _build_registry(modules_repo: Path, output: Path) -> str:
    git = shutil.which("git")
    go = shutil.which("go")
    if git is None or go is None:
        raise RuntimeError("git and go must be available to build the registry probe")
    revision = subprocess.run(  # noqa: S603
        [git, "rev-parse", "HEAD"],
        cwd=modules_repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(  # noqa: S603
        [go, "build", "-o", str(output), "./cmd/livepeer-service-registry-daemon"],
        cwd=modules_repo / "service-registry-daemon",
        check=True,
    )
    return revision


def _select(stub: Any, capability: str) -> Any:
    result = stub.Select(
        resolver_pb2.SelectRequest(capability=capability, offering="default"), timeout=5
    )
    if not result.HasField("route"):
        raise AssertionError(f"Select returned no route for {capability}")
    return result.route


def _wait_for_channel(
    channel: grpc.Channel, daemon: subprocess.Popen[str], daemon_log_path: Path
) -> None:
    ready = grpc.channel_ready_future(channel)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if daemon.poll() is not None:
            log = daemon_log_path.read_text() if daemon_log_path.exists() else ""
            raise RuntimeError(f"service-registry-daemon exited with {daemon.returncode}:\n{log}")
        try:
            ready.result(timeout=0.25)
            return
        except grpc.FutureTimeoutError:
            continue
    raise TimeoutError("service-registry-daemon gRPC socket was not ready within 15 seconds")


def run(modules_repo: Path, artifacts: Path) -> dict[str, Any]:
    modules_repo = modules_repo.resolve()
    artifacts.mkdir(parents=True, exist_ok=True)
    daemon_log_path = artifacts / "service-registry-daemon.log"

    with tempfile.TemporaryDirectory(prefix="loc-registry-seed-") as raw_tmp:
        runtime = Path(raw_tmp)
        registry_bin = runtime / "livepeer-service-registry-daemon"
        revision = _build_registry(modules_repo, registry_bin)

        handler = partial(_QuietHandler, directory=str(runtime))
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()

        worker_url = f"http://127.0.0.1:{server.server_port}"
        manifest_path = runtime / "manifest.json"
        manifest_path.write_text(json.dumps(_signed_manifest(worker_url), indent=2) + "\n")
        health_dir = runtime / "registry"
        health_dir.mkdir()
        (health_dir / "health").write_text(
            json.dumps(
                {
                    "broker_status": "ready",
                    "generated_at": _rfc3339(datetime.now(UTC)),
                    "capabilities": [
                        {
                            "id": capability,
                            "offering_id": "default",
                            "status": "ready",
                            "stale_after": _rfc3339(datetime.now(UTC) + timedelta(minutes=5)),
                        }
                        for capability in ("test:job", "test:session")
                    ],
                },
                indent=2,
            )
            + "\n"
        )

        service_uri = f"http://127.0.0.1:{server.server_port}/{manifest_path.name}"
        seed_path = runtime / "seed.yaml"
        seed_path.write_text(
            "seed:\n"
            f'  - eth_address: "{COLD_KEY.public_key.to_address()}"\n'
            f'    service_uri: "{service_uri}"\n'
        )
        socket_path = runtime / "registry.sock"

        with daemon_log_path.open("w") as daemon_log:
            daemon = subprocess.Popen(  # noqa: S603
                [
                    str(registry_bin),
                    "--mode=resolver",
                    "--dev",
                    f"--chain-seed={seed_path}",
                    f"--socket={socket_path}",
                    "--metrics-listen=",
                ],
                cwd=modules_repo / "service-registry-daemon",
                stdout=daemon_log,
                stderr=subprocess.STDOUT,
                text=True,
            )
            channel = grpc.insecure_channel(f"unix://{socket_path}")
            try:
                _wait_for_channel(channel, daemon, daemon_log_path)
                stub = resolver_pb2_grpc.ResolverStub(channel)
                stub.ResolveByAddress(
                    resolver_pb2.ResolveByAddressRequest(
                        eth_address=COLD_KEY.public_key.to_address(), force_refresh=True
                    ),
                    timeout=5,
                )
                job = _select(stub, "test:job")
                session = _select(stub, "test:session")

                job_extra = json.loads(job.extra_json)
                session_extra = json.loads(session.extra_json)
                expected_key = "0x04" + SETTLEMENT_KEY.public_key.to_bytes().hex()
                assert job.protocol == "paid-job/v1"
                assert job_extra["job"]["transports"] == ["unary", "stream"]
                assert job.units_per_price == 1000
                assert session.protocol == "paid-session/v1"
                assert session_extra["session"]["descriptor_schema"] == "test-runtime/v1"
                assert len(job.settlement_keys) == 1
                assert len(session.settlement_keys) == 1
                assert job.settlement_keys[0].public_key == expected_key
                assert session.settlement_keys[0].public_key == expected_key

                result = {
                    "status": "ok",
                    "modules_revision": revision,
                    "registry_mode": "signed-chain-seed",
                    "operator_address": COLD_KEY.public_key.to_address(),
                    "job_protocol": job.protocol,
                    "session_protocol": session.protocol,
                    "settlement_public_key": expected_key,
                }
                (artifacts / "result.json").write_text(json.dumps(result, indent=2) + "\n")
                return result
            finally:
                channel.close()
                daemon.terminate()
                try:
                    daemon.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    daemon.kill()
                    daemon.wait(timeout=5)
                server.shutdown()
                server.server_close()
                server_thread.join(timeout=5)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--modules-repo",
        type=Path,
        default=Path(__file__).resolve().parents[3] / "livepeer-network-modules",
    )
    parser.add_argument(
        "--artifacts",
        type=Path,
        default=Path(".artifacts/live-conformance/registry-seed"),
    )
    args = parser.parse_args()
    started = time.monotonic()
    result = run(args.modules_repo, args.artifacts)
    result["elapsed_seconds"] = round(time.monotonic() - started, 3)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
