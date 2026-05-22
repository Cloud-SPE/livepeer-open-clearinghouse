#!/usr/bin/env python3
"""Generate a V3 Ethereum keystore for local payment-daemon testing.

Writes:
    .dev/keystore/keystore.json
    .dev/keystore/keystore-password

After running, point `payment-daemon`'s `--keystore-path` and
`--keystore-password-file` at those files (see docker-compose.yml comments).

This script is only useful when you want payment-daemon to actually sign
against a chain. The default dev compose uses the daemon's deterministic
dev key — you don't need this script for the default flow.

Run:
    uv run python scripts/dev-keystore.py
or:
    make dev-keystore
"""

from __future__ import annotations

import json
import secrets
import sys
from pathlib import Path


def main() -> int:
    try:
        from eth_account import Account
    except ImportError:
        print(
            "Missing dependency: `eth-account`. Install with:\n"
            "    uv add --dev eth-account\n",
            file=sys.stderr,
        )
        return 1

    out_dir = Path(__file__).resolve().parents[1] / ".dev" / "keystore"
    out_dir.mkdir(parents=True, exist_ok=True)

    keystore_path = out_dir / "keystore.json"
    password_path = out_dir / "keystore-password"

    if keystore_path.exists() or password_path.exists():
        print(
            f"Refusing to overwrite existing keystore in {out_dir}.\n"
            f"Delete the files first if you want a fresh one.",
            file=sys.stderr,
        )
        return 1

    password = secrets.token_urlsafe(32)
    account = Account.create()
    encrypted = Account.encrypt(account.key, password)

    keystore_path.write_text(json.dumps(encrypted, indent=2))
    keystore_path.chmod(0o600)

    # The daemon expects the password as the raw file content (no trailing newline).
    password_path.write_text(password)
    password_path.chmod(0o600)

    print(f"Wrote {keystore_path}")
    print(f"Wrote {password_path}")
    print(f"Wallet address: {account.address}")
    print()
    print("Add these to docker-compose.yml's payment-daemon section:")
    print("  command:")
    print("    - --chain-rpc=${CHAIN_RPC}")
    print("    - --keystore-path=/etc/livepeer/keystore.json")
    print("    - --keystore-password-file=/etc/livepeer/keystore-password")
    print("  volumes:")
    print("    - ./.dev/keystore/keystore.json:/etc/livepeer/keystore.json:ro")
    print("    - ./.dev/keystore/keystore-password:/etc/livepeer/keystore-password:ro")
    return 0


if __name__ == "__main__":
    sys.exit(main())
