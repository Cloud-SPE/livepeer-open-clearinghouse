"""Rehearse 0029 using only a fresh, disposable PostgreSQL container.

Run with ``uv run python scripts/test_account_isolation_migration.py``.
Never accepts a database URL; existing databases cannot be targeted.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import asyncpg

ROOT = Path(__file__).resolve().parents[1]


def run(*args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, text=True, capture_output=True, check=True, **kwargs)


def migrate(url: str, revision: str, *, blocked: bool = False) -> None:
    result = subprocess.run(
        [str(ROOT / ".venv/bin/alembic"), "upgrade", revision],
        cwd=ROOT,
        env={**os.environ, "DATABASE_URL": url, "PAYMENT_DAEMON_MODE": "mock", "APP_ENV": "dev"},
        text=True,
        capture_output=True,
    )
    if blocked:
        assert result.returncode != 0, "unsafe migration unexpectedly succeeded"
        assert "Drain and reconcile" in result.stderr, result.stderr
    else:
        assert result.returncode == 0, result.stderr


async def seed(db: asyncpg.Connection, table: str, **values: object) -> dict[str, object]:
    """Fill required fixture fields while preserving real foreign-key constraints."""
    columns = await db.fetch(
        """
        SELECT column_name, data_type FROM information_schema.columns
        WHERE table_schema='public' AND table_name=$1
        AND is_nullable='NO' AND column_default IS NULL ORDER BY ordinal_position
    """,
        table,
    )
    for column in columns:
        name, kind = column["column_name"], column["data_type"]
        if name in values:
            continue
        if kind == "uuid":
            value = uuid.uuid4()
        elif kind in ("integer", "bigint", "numeric"):
            value = 1
        elif kind.startswith("timestamp"):
            value = datetime.now(UTC)
        elif kind == "bytea":
            value = b"fixture"
        elif kind in ("json", "jsonb"):
            value = "{}"
        elif kind == "boolean":
            value = False
        else:
            value = "fixture"
        values[name] = value
    names = ", ".join(f'"{name}"' for name in values)
    params = ", ".join(f"${n}" for n in range(1, len(values) + 1))
    await db.execute(f'INSERT INTO "{table}" ({names}) VALUES ({params})', *values.values())
    return values


async def verify(url: str, pg_url: str) -> None:
    migrate(url, "0028")
    db = await asyncpg.connect(pg_url)
    try:
        user = await seed(db, "user", email="fixture@example.invalid")
        api_key = await seed(db, "api_key", user_id=user["id"])
        session = await seed(
            db, "payment_session", user_id=user["id"], api_key_id=api_key["id"], state="closed"
        )
        await seed(db, "spend_authorization_grant", session_id=session["id"], state="issued")
        account = await seed(
            db,
            "wholesale_account",
            chain_id=42161,
            payer_eth_address="0x" + "aa" * 20,
            payee_eth_address="0x" + "bb" * 20,
            settlement_domain_id="0x" + "cc" * 32,
            denomination="wei",
            credited_value_wei=100,
            available_value_wei=70,
            reserved_value_wei=10,
            debited_value_wei=20,
        )
        funding = await seed(db, "wholesale_funding", account_id=account["id"], status="minted")
        for state in ("funding-and-grant", "grant-only"):
            migrate(url, "head", blocked=True)
            assert await db.fetchval("SELECT version_num FROM alembic_version") == "0028"
            assert (
                await db.fetchval(
                    "SELECT count(*) FROM information_schema.columns WHERE table_name='wholesale_account' AND column_name='wholesale_account_id'"
                )
                == 0
            )
            await db.execute("UPDATE wholesale_funding SET status='acknowledged'")
        await db.execute("UPDATE spend_authorization_grant SET state='settled'")
        migrate(url, "head")
        assert await db.fetchval("SELECT version_num FROM alembic_version") == "0029"
        legacy = await db.fetchrow(
            "SELECT wholesale_account_id, available_value_wei, credited_value_wei FROM wholesale_account"
        )
        assert tuple(legacy) == ("", 70, 100)
        assert await db.fetchval("SELECT wholesale_account_id FROM spend_authorization_grant") == ""
        assert (
            await db.fetchval("SELECT account_id FROM wholesale_funding WHERE id=$1", funding["id"])
            == account["id"]
        )
        for label in ("loc-prod", "loc-dev-test", "blueclaw-prod"):
            isolated = {**account, "id": uuid.uuid4(), "wholesale_account_id": label}
            await seed(db, "wholesale_account", **isolated)
        try:
            await seed(
                db,
                "wholesale_account",
                **{**account, "id": uuid.uuid4(), "wholesale_account_id": "loc-prod"},
            )
        except asyncpg.UniqueViolationError:
            pass
        else:
            raise AssertionError("duplicate account namespace was accepted")
        assert await db.fetchval("SELECT count(*) FROM wholesale_account") == 4
        print(
            json.dumps(
                {
                    "status": "passed",
                    "revision": "0029",
                    "checks": [
                        "pending funding and active grants block transactionally",
                        "legacy namespace and money retained",
                        "funding foreign key retained",
                        "three labels coexist",
                        "duplicate identity rejected",
                    ],
                }
            )
        )
    finally:
        await db.close()


def main() -> None:
    name = "loc-account-isolation-test-" + uuid.uuid4().hex[:12]
    run(
        "docker",
        "run",
        "-d",
        "--rm",
        "--name",
        name,
        "--tmpfs",
        "/var/lib/postgresql/data",
        "-p",
        "127.0.0.1::5432",
        "-e",
        "POSTGRES_PASSWORD=isolated-test",
        "postgres:16-alpine",
    )
    try:
        for _ in range(60):
            result = subprocess.run(
                ["docker", "exec", name, "pg_isready", "-U", "postgres"], capture_output=True
            )
            if result.returncode == 0:
                break
            time.sleep(0.25)
        else:
            raise RuntimeError("temporary PostgreSQL did not become ready")
        port = run("docker", "port", name, "5432/tcp").stdout.strip().rsplit(":", 1)[1]
        pg_url = f"postgresql://postgres:isolated-test@127.0.0.1:{port}/postgres"
        asyncio.run(verify(pg_url.replace("postgresql://", "postgresql+asyncpg://"), pg_url))
    finally:
        run("docker", "rm", "-f", name)


if __name__ == "__main__":
    main()
