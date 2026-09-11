#!/usr/bin/env python3
"""Audit LOC's disabled-to-enabled wholesale schema migration."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import asyncpg  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field

DATABASE_ENV = "DATABASE_URL"
EXPECTED_SOURCE_REVISION = "0024"
LEGACY_TABLES = (
    "api_key",
    "credit_balance",
    "credit_ledger",
    "credit_topup",
    "payment",
    "payment_idempotency_key",
    "payment_session",
    "payment_settlement",
    "spend_window",
    "user",
)
GROUPS = {
    ("payment", "status"),
    ("payment_session", "protocol"),
    ("payment_session", "state"),
}


class FinancialTotals(BaseModel):
    model_config = ConfigDict(extra="forbid")

    credit_balance_wei: str
    credit_ledger_wei: str
    payment_funded_wei: str
    payment_reserved_wei: str
    payment_refunded_wei: str
    session_funded_wei: str
    session_billed_wei: str
    settlement_billed_wei: str


class WholesaleInitialization(BaseModel):
    model_config = ConfigDict(extra="forbid")

    non_legacy_sessions: int = 0
    sessions_with_new_customer_fields: int = 0
    linked_legacy_ledger_rows: int = 0
    wholesale_accounts: int = 0
    wholesale_fundings: int = 0
    authorization_grants: int = 0
    global_budget_rows: int = 0
    global_projected_available_wei: str = "0"
    non_global_budget_rows: int = 0


class AuditReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format_version: Literal[1] = 1
    phase: Literal["pre", "post"]
    captured_at: datetime
    database_name: str
    server_version: str
    alembic_revision: str
    table_counts: dict[str, int]
    payment_status_counts: dict[str, int]
    session_state_counts: dict[str, int]
    session_protocol_counts: dict[str, int]
    financial_totals: FinancialTotals
    wholesale_initialization: WholesaleInitialization | None = None
    blockers: list[str] = Field(default_factory=list)


class ComparisonReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format_version: Literal[1] = 1
    compared_at: datetime
    before_revision: str
    after_revision: str
    table_count_changes: dict[str, dict[str, int]]
    categorical_changes: dict[str, dict[str, dict[str, int]]]
    financial_changes: dict[str, dict[str, str]]
    failures: list[str]


def _asyncpg_url(raw_url: str) -> str:
    return raw_url.replace("postgresql+asyncpg://", "postgresql://", 1)


def _decimal_text(value: Decimal | int | None) -> str:
    return str(value or 0)


async def _group_counts(
    connection: asyncpg.Connection[Any], table: str, column: str
) -> dict[str, int]:
    if (table, column) not in GROUPS:
        raise ValueError(f"unsupported grouped count: {table}.{column}")
    rows = await connection.fetch(
        f'SELECT "{column}"::text AS value, COUNT(*)::bigint AS count '  # noqa: S608
        f'FROM "{table}" GROUP BY "{column}" ORDER BY "{column}"'
    )
    return {str(row["value"]): int(row["count"]) for row in rows}


async def _present_tables(connection: asyncpg.Connection[Any]) -> set[str]:
    return {
        str(row["tablename"])
        for row in await connection.fetch(
            """
            SELECT tablename FROM pg_catalog.pg_tables
            WHERE schemaname = 'public' ORDER BY tablename
            """
        )
    }


async def _post_initialization(
    connection: asyncpg.Connection[Any], present: set[str]
) -> WholesaleInitialization:
    required = {
        "wholesale_account",
        "wholesale_exposure_budget",
        "wholesale_funding",
        "spend_authorization_grant",
    }
    missing = sorted(required - present)
    if missing:
        raise RuntimeError(f"post-migration database is missing tables: {', '.join(missing)}")
    row = await connection.fetchrow(
        """
        SELECT
          (SELECT COUNT(*) FROM payment_session
           WHERE accounting_mode <> 'legacy_ticket') AS non_legacy_sessions,
          (SELECT COUNT(*) FROM payment_session
           WHERE customer_pricing IS NOT NULL
              OR customer_max_debit_wei IS NOT NULL
              OR authorization_id IS NOT NULL) AS sessions_with_new_customer_fields,
          (SELECT COUNT(*) FROM credit_ledger
           WHERE related_engagement_id IS NOT NULL) AS linked_legacy_ledger_rows,
          (SELECT COUNT(*) FROM wholesale_account) AS wholesale_accounts,
          (SELECT COUNT(*) FROM wholesale_funding) AS wholesale_fundings,
          (SELECT COUNT(*) FROM spend_authorization_grant) AS authorization_grants,
          (SELECT COUNT(*) FROM wholesale_exposure_budget
           WHERE scope = 'global') AS global_budget_rows,
          (SELECT COALESCE(SUM(projected_available_wei), 0)
           FROM wholesale_exposure_budget WHERE scope = 'global')
             AS global_projected_available_wei,
          (SELECT COUNT(*) FROM wholesale_exposure_budget
           WHERE scope <> 'global') AS non_global_budget_rows
        """
    )
    assert row is not None
    values = dict(row)
    values["global_projected_available_wei"] = _decimal_text(
        values["global_projected_available_wei"]
    )
    return WholesaleInitialization.model_validate(values)


async def collect_report(database_url: str, phase: Literal["pre", "post"]) -> AuditReport:
    connection = await asyncpg.connect(_asyncpg_url(database_url))
    try:
        present = await _present_tables(connection)
        missing = sorted(set(LEGACY_TABLES) - present)
        if missing:
            raise RuntimeError(f"database is missing legacy tables: {', '.join(missing)}")
        counts = {
            table: int(await connection.fetchval(f'SELECT COUNT(*) FROM "{table}"'))  # noqa: S608
            for table in LEGACY_TABLES
        }
        financial_row = await connection.fetchrow(
            """
            SELECT
              (SELECT COALESCE(SUM(amount_wei), 0) FROM credit_balance)
                AS credit_balance_wei,
              (SELECT COALESCE(SUM(delta_wei), 0) FROM credit_ledger)
                AS credit_ledger_wei,
              (SELECT COALESCE(SUM(funded_value_wei), 0) FROM payment)
                AS payment_funded_wei,
              (SELECT COALESCE(SUM(reserved_wei), 0) FROM payment)
                AS payment_reserved_wei,
              (SELECT COALESCE(SUM(refunded_wei), 0) FROM payment)
                AS payment_refunded_wei,
              (SELECT COALESCE(SUM(funded_value_wei), 0) FROM payment_session)
                AS session_funded_wei,
              (SELECT COALESCE(SUM(billed_value_wei), 0) FROM payment_session)
                AS session_billed_wei,
              (SELECT COALESCE(SUM(billed_value_wei), 0) FROM payment_settlement)
                AS settlement_billed_wei
            """
        )
        assert financial_row is not None
        revision = str(await connection.fetchval("SELECT version_num FROM alembic_version"))
        payment_statuses = await _group_counts(connection, "payment", "status")
        session_states = await _group_counts(connection, "payment_session", "state")
        initialization = (
            await _post_initialization(connection, present) if phase == "post" else None
        )
        blockers: list[str] = []
        if phase == "pre":
            if revision != EXPECTED_SOURCE_REVISION:
                blockers.append(
                    f"expected source revision {EXPECTED_SOURCE_REVISION}, found {revision}"
                )
            active_sessions = sum(session_states.get(value, 0) for value in ("open", "draining"))
            if active_sessions:
                blockers.append(f"{active_sessions} legacy sessions are open or draining")
            active_payments = int(
                await connection.fetchval(
                    """
                    SELECT COUNT(*)
                    FROM payment AS p
                    LEFT JOIN payment_session AS s ON s.id = p.session_id
                    WHERE p.status IN ('reserved', 'issued')
                      AND (p.session_id IS NULL OR s.state IN ('open', 'draining'))
                    """
                )
            )
            if active_payments:
                blockers.append(f"{active_payments} legacy payments are reserved or issued")
        elif initialization is not None:
            expected_zero = initialization.model_dump(exclude={"global_budget_rows"})
            for name, value in expected_zero.items():
                if value not in (0, "0"):
                    blockers.append(f"wholesale initialization {name} is {value}, expected zero")
            if initialization.global_budget_rows != 1:
                blockers.append(
                    "wholesale initialization requires exactly one zero-valued global budget row"
                )

        return AuditReport(
            phase=phase,
            captured_at=datetime.now(UTC),
            database_name=str(await connection.fetchval("SELECT current_database()")),
            server_version=str(await connection.fetchval("SHOW server_version")),
            alembic_revision=revision,
            table_counts=counts,
            payment_status_counts=payment_statuses,
            session_state_counts=session_states,
            session_protocol_counts=await _group_counts(connection, "payment_session", "protocol"),
            financial_totals=FinancialTotals(
                **{key: _decimal_text(financial_row[key]) for key in FinancialTotals.model_fields}
            ),
            wholesale_initialization=initialization,
            blockers=blockers,
        )
    finally:
        await connection.close()


def _category_changes(before: dict[str, int], after: dict[str, int]) -> dict[str, dict[str, int]]:
    return {
        value: {"before": before.get(value, 0), "after": after.get(value, 0)}
        for value in sorted(set(before) | set(after))
        if before.get(value, 0) != after.get(value, 0)
    }


def compare_reports(
    before: AuditReport, after: AuditReport, expected_after_revision: str
) -> ComparisonReport:
    failures = [f"pre-migration blocker: {item}" for item in before.blockers]
    failures.extend(f"post-migration blocker: {item}" for item in after.blockers)
    table_changes: dict[str, dict[str, int]] = {}
    for table, before_count in sorted(before.table_counts.items()):
        after_count = after.table_counts.get(table, -1)
        if before_count != after_count:
            table_changes[table] = {"before": before_count, "after": after_count}
            failures.append(f"row count changed for {table}: {before_count} -> {after_count}")
    categorical_changes: dict[str, dict[str, dict[str, int]]] = {}
    for name in ("payment_status_counts", "session_state_counts", "session_protocol_counts"):
        changes = _category_changes(getattr(before, name), getattr(after, name))
        if changes:
            categorical_changes[name] = changes
            failures.append(f"categorical counts changed for {name}")
    financial_changes: dict[str, dict[str, str]] = {}
    for name, before_value in before.financial_totals.model_dump().items():
        after_value = getattr(after.financial_totals, name)
        if before_value != after_value:
            financial_changes[name] = {"before": before_value, "after": after_value}
            failures.append(f"financial total changed for {name}: {before_value} -> {after_value}")
    if after.alembic_revision != expected_after_revision:
        failures.append(
            f"expected post-migration revision {expected_after_revision}, "
            f"found {after.alembic_revision}"
        )
    return ComparisonReport(
        compared_at=datetime.now(UTC),
        before_revision=before.alembic_revision,
        after_revision=after.alembic_revision,
        table_count_changes=table_changes,
        categorical_changes=categorical_changes,
        financial_changes=financial_changes,
        failures=failures,
    )


def _write(path: Path, model: BaseModel) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(model.model_dump_json(indent=2) + "\n", encoding="utf-8")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    snapshot = commands.add_parser("snapshot")
    snapshot.add_argument("--phase", choices=("pre", "post"), required=True)
    snapshot.add_argument("--output", type=Path, required=True)
    compare = commands.add_parser("compare")
    compare.add_argument("--before", type=Path, required=True)
    compare.add_argument("--after", type=Path, required=True)
    compare.add_argument("--expected-after-revision", required=True)
    compare.add_argument("--output", type=Path, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.command == "snapshot":
        database_url = os.environ.get(DATABASE_ENV)
        if not database_url:
            print(f"error: {DATABASE_ENV} must be set", file=sys.stderr)
            return 2
        report = asyncio.run(collect_report(database_url, args.phase))
        _write(args.output, report)
        for blocker in report.blockers:
            print(f"BLOCKER: {blocker}", file=sys.stderr)
        return 1 if report.blockers else 0
    before = AuditReport.model_validate_json(args.before.read_text(encoding="utf-8"))
    after = AuditReport.model_validate_json(args.after.read_text(encoding="utf-8"))
    result = compare_reports(before, after, args.expected_after_revision)
    _write(args.output, result)
    for failure in result.failures:
        print(f"FAIL: {failure}", file=sys.stderr)
    return 1 if result.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
