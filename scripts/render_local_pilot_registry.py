#!/usr/bin/env python3
"""Render a signed registry manifest for the localhost integration pilot.

The Modules integration stack exposes its broker catalog but deliberately
leaves the trusted resolver beside LOC.  This helper turns that catalog into
the ordinary signed manifest consumed by service-registry-daemon.  Private
keys are read only for signing and are never copied into the output directory.
"""

from __future__ import annotations

import argparse
import json
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import rfc8785
from eth_account import Account
from eth_hash.auto import keccak
from eth_keys.datatypes import PrivateKey


def _rfc3339(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _read_json(url: str) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310 - operator URL
        value = json.load(response)
    if not isinstance(value, dict):
        raise TypeError(f"{url} returned {type(value).__name__}, expected object")
    return value


def _sign_personal(payload: bytes, private_key: PrivateKey) -> str:
    prefix = f"\x19Ethereum Signed Message:\n{len(payload)}".encode()
    signature = bytearray(private_key.sign_msg_hash(keccak(prefix + payload)).to_bytes())
    signature[64] += 27
    return "0x" + signature.hex()


def _capability(entry: dict[str, Any], broker_url: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "capability_id": entry["capability_id"],
        "offering_id": entry["offering_id"],
        "protocol": entry["protocol"],
        "work_unit": entry["work_unit"],
        "price_per_unit_wei": entry["price_per_unit_wei"],
        "per_units": entry.get("per_units", 1),
        "worker_url": broker_url,
    }
    for key in ("job", "session", "extra", "constraints"):
        value = entry.get(key)
        if value:
            result[key] = value
    return result


def render(args: argparse.Namespace) -> dict[str, str]:
    catalog = _read_json(args.offerings_url)
    entries = catalog.get("capabilities")
    if not isinstance(entries, list) or not entries:
        raise ValueError("broker catalog has no capabilities")

    password = args.orch_keystore_password_file.read_text().strip()
    keystore = json.loads(args.orch_keystore.read_text())
    orch_key = PrivateKey(Account.decrypt(keystore, password))
    orch_address = orch_key.public_key.to_address().lower()
    advertised_address = str(catalog.get("orch_eth_address", "")).lower()
    if advertised_address != orch_address:
        raise ValueError(
            f"broker advertises {advertised_address}, keystore belongs to {orch_address}"
        )

    settlement_key = PrivateKey(bytes.fromhex(args.settlement_key.read_text().strip()))
    settlement_public_key = "0x04" + settlement_key.public_key.to_bytes().hex()

    now = datetime.now(UTC).replace(microsecond=0)
    expires = now + timedelta(days=args.valid_days)
    manifest: dict[str, Any] = {
        "spec_version": "1.0.0",
        "publication_seq": int(now.timestamp()),
        "issued_at": _rfc3339(now),
        "expires_at": _rfc3339(expires),
        "orch": {
            "eth_address": orch_address,
            "service_uri": args.service_uri,
        },
        "settlement_keys": [
            {
                "public_key": settlement_public_key,
                "not_before": _rfc3339(now - timedelta(hours=1)),
                "expires_at": _rfc3339(expires),
            }
        ],
        "capabilities": [_capability(entry, args.broker_url) for entry in entries],
    }
    envelope = {
        "manifest": manifest,
        "signature": {
            "algorithm": "secp256k1",
            "canonicalization": "JCS",
            "value": _sign_personal(rfc8785.dumps(manifest), orch_key),
        },
    }

    args.output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = args.output_dir / "manifest.json"
    seed_path = args.output_dir / "chain-seed.yaml"
    manifest_path.write_text(json.dumps(envelope, indent=2) + "\n")
    seed_path.write_text(
        "seed:\n"
        f'  - eth_address: "{orch_address}"\n'
        f'    service_uri: "{args.service_uri}"\n'
    )
    return {
        "orch_address": orch_address,
        "settlement_public_key": settlement_public_key,
        "manifest": str(manifest_path),
        "chain_seed": str(seed_path),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--offerings-url", required=True)
    parser.add_argument("--broker-url", required=True)
    parser.add_argument("--service-uri", required=True)
    parser.add_argument("--orch-keystore", type=Path, required=True)
    parser.add_argument("--orch-keystore-password-file", type=Path, required=True)
    parser.add_argument("--settlement-key", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--valid-days", type=int, default=30)
    result = render(parser.parse_args())
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
