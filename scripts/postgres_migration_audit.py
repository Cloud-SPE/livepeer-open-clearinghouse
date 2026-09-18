#!/usr/bin/env python3
"""Audit and compare LOC PostgreSQL databases around the v1-to-v2 migration."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import asyncpg
from pydantic import BaseModel, ConfigDict, Field

DATABASE_ENV = "DATABASE_URL"
EXPECTED_V1_REVISION = "0013"
V1_TABLES = (
    "api_key",
    "credit_balance",
    "credit_ledger",
    "credit_topup",
    "email_event",
    "email_send",
    "notification_config",
    "notification_webhook_config",
    "operator",
    "operator_approval",
    "operator_audit",
    "payment",
    "payment_daemon_deposit_snapshot",
    "payment_idempotency_key",
    "payment_session",
    "payment_settlement",
    "portal_notification",
    "sdk_approval",
    "spend_window",
    "telemetry_event",
    "usage_record",
    "user",
    "user_billing_config",
    "user_email_verification",
    "user_oauth_identity",
    "user_password_reset",
    "user_session",
)
GROUP_COUNT_FIELDS = {
    ("payment", "status"),
    ("payment_idempotency_key", "status"),
    ("payment_session", "mode"),
    ("payment_session", "protocol"),
    ("payment_session", "state"),
}


class FinancialTotals(BaseModel):
    model_config = ConfigDict(extra="forbid")

    credit_balance_wei: str
    credit_ledger_wei: str
    credit_topup_wei: str
    payment_funded_wei: str
    payment_expected_wei: str
    payment_reserved_wei: str
    payment_refunded_wei: str
    session_funded_wei: str
    session_billed_wei: str
    settlement_billed_wei: str
    spend_window_spent_wei: str


class InvariantViolations(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ledger_balance_mismatches: int
    negative_balances: int
    invalid_payment_refunds: int
    orphan_payments: int
    orphan_sessions: int
    orphan_settlements: int


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
    idempotency_status_counts: dict[str, int]
    financial_totals: FinancialTotals
    invariant_violations: InvariantViolations
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
    invariant_violations_after: InvariantViolations
    failures: list[str]


def _asyncpg_url(raw_url: str) -> str:
    return raw_url.replace("postgresql+asyncpg://", "postgresql://", 1)


def _decimal_text(value: Decimal | int | None) -> str:
    return str(value or 0)


async def _scalar(connection: asyncpg.Connection[Any], query: str) -> Any:
    return await connection.fetchval(query)


async def _group_counts(
    connection: asyncpg.Connection[Any], table: str, column: str
) -> dict[str, int]:
    if (table, column) not in GROUP_COUNT_FIELDS:
        raise ValueError(f"unsupported grouped count: {table}.{column}")
    # Identifiers are selected only from GROUP_COUNT_FIELDS above.
    query = (
        f'SELECT "{column}"::text AS value, COUNT(*)::bigint AS count '  # noqa: S608
        f'FROM "{table}" GROUP BY "{column}" ORDER BY "{column}"'
    )
    rows = await connection.fetch(query)
    return {str(row["value"]): int(row["count"]) for row in rows}


async def _column_exists(connection: asyncpg.Connection[Any], table: str, column: str) -> bool:
    return bool(
        await connection.fetchval(
            """
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = $1
                  AND column_name = $2
            )
            """,
            table,
            column,
        )
    )


async def collect_report(database_url: str, phase: Literal["pre", "post"]) -> AuditReport:
    connection = await asyncpg.connect(_asyncpg_url(database_url))
    try:
        present_tables = {
            str(row["tablename"])
            for row in await connection.fetch(
                """
                SELECT tablename
                FROM pg_catalog.pg_tables
                WHERE schemaname = 'public'
                ORDER BY tablename
                """
            )
        }
        missing_tables = sorted(set(V1_TABLES) - present_tables)
        if missing_tables:
            missing = ", ".join(missing_tables)
            raise RuntimeError(f"database is missing expected v1 tables: {missing}")
        table_counts = {
            # Table names are selected only from the module-owned V1_TABLES tuple.
            table: int(
                await _scalar(connection, f'SELECT COUNT(*) FROM "{table}"')  # noqa: S608
            )
            for table in V1_TABLES
        }

        protocol_column = (
            "protocol"
            if await _column_exists(connection, "payment_session", "protocol")
            else "mode"
        )
        financial_row = await connection.fetchrow(
            """
            SELECT
              (SELECT COALESCE(SUM(amount_wei), 0) FROM credit_balance) AS credit_balance_wei,
              (SELECT COALESCE(SUM(delta_wei), 0) FROM credit_ledger) AS credit_ledger_wei,
              (SELECT COALESCE(SUM(amount_wei), 0) FROM credit_topup) AS credit_topup_wei,
              (SELECT COALESCE(SUM(funded_value_wei), 0) FROM payment) AS payment_funded_wei,
              (SELECT COALESCE(SUM(expected_value_wei), 0) FROM payment) AS payment_expected_wei,
              (SELECT COALESCE(SUM(reserved_wei), 0) FROM payment) AS payment_reserved_wei,
              (SELECT COALESCE(SUM(refunded_wei), 0) FROM payment) AS payment_refunded_wei,
              (SELECT COALESCE(SUM(funded_value_wei), 0) FROM payment_session)
                AS session_funded_wei,
              (SELECT COALESCE(SUM(billed_value_wei), 0) FROM payment_session)
                AS session_billed_wei,
              (SELECT COALESCE(SUM(billed_value_wei), 0) FROM payment_settlement)
                AS settlement_billed_wei,
              (SELECT COALESCE(SUM(spent_wei), 0) FROM spend_window) AS spend_window_spent_wei
            """
        )
        assert financial_row is not None
        financial_totals = FinancialTotals(
            **{key: _decimal_text(financial_row[key]) for key in FinancialTotals.model_fields}
        )

        violation_row = await connection.fetchrow(
            """
            WITH ledger_by_user AS (
              SELECT user_id, SUM(delta_wei) AS amount_wei
              FROM credit_ledger
              GROUP BY user_id
            )
            SELECT
              (
                SELECT COUNT(*) FROM (
                  SELECT COALESCE(b.user_id, l.user_id) AS user_id
                  FROM credit_balance b
                  FULL OUTER JOIN ledger_by_user l USING (user_id)
                  WHERE COALESCE(b.amount_wei, 0) <> COALESCE(l.amount_wei, 0)
                ) mismatch
              ) AS ledger_balance_mismatches,
              (SELECT COUNT(*) FROM credit_balance WHERE amount_wei < 0) AS negative_balances,
              (
                SELECT COUNT(*) FROM payment
                WHERE refunded_wei < 0 OR refunded_wei > reserved_wei
              ) AS invalid_payment_refunds,
              (
                SELECT COUNT(*) FROM payment p
                LEFT JOIN "user" u ON u.id = p.user_id
                LEFT JOIN api_key k ON k.id = p.api_key_id
                WHERE u.id IS NULL OR k.id IS NULL
              ) AS orphan_payments,
              (
                SELECT COUNT(*) FROM payment_session s
                LEFT JOIN "user" u ON u.id = s.user_id
                LEFT JOIN api_key k ON k.id = s.api_key_id
                WHERE u.id IS NULL OR k.id IS NULL
              ) AS orphan_sessions,
              (
                SELECT COUNT(*) FROM payment_settlement x
                LEFT JOIN payment_session s ON s.id = x.session_id
                WHERE s.id IS NULL
              ) AS orphan_settlements
            """
        )
        assert violation_row is not None
        violations = InvariantViolations(
            **{key: int(violation_row[key]) for key in InvariantViolations.model_fields}
        )

        revision = str(await _scalar(connection, "SELECT version_num FROM alembic_version"))
        payment_statuses = await _group_counts(connection, "payment", "status")
        session_states = await _group_counts(connection, "payment_session", "state")
        idempotency_statuses = await _group_counts(connection, "payment_idempotency_key", "status")
        blockers: list[str] = []
        if phase == "pre" and revision != EXPECTED_V1_REVISION:
            blockers.append(
                f"expected v1 Alembic revision {EXPECTED_V1_REVISION}, found {revision}"
            )
        if phase == "pre":
            active_sessions = sum(session_states.get(state, 0) for state in ("open", "draining"))
            if active_sessions:
                blockers.append(f"{active_sessions} v1 payment sessions are open or draining")
            pending_payments = sum(
                payment_statuses.get(status, 0) for status in ("reserved", "issued")
            )
            if pending_payments:
                blockers.append(f"{pending_payments} v1 payments are reserved or issued")
            idempotency_rows = sum(idempotency_statuses.values())
            if idempotency_rows:
                blockers.append(
                    f"{idempotency_rows} legacy idempotency rows would be destroyed "
                    "by migration 0014"
                )
        for name, count in violations.model_dump().items():
            if count:
                blockers.append(f"financial/referential invariant {name} has {count} violation(s)")

        return AuditReport(
            phase=phase,
            captured_at=datetime.now(UTC),
            database_name=str(await _scalar(connection, "SELECT current_database()")),
            server_version=str(await _scalar(connection, "SHOW server_version")),
            alembic_revision=revision,
            table_counts=table_counts,
            payment_status_counts=payment_statuses,
            session_state_counts=session_states,
            session_protocol_counts=await _group_counts(
                connection, "payment_session", protocol_column
            ),
            idempotency_status_counts=idempotency_statuses,
            financial_totals=financial_totals,
            invariant_violations=violations,
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
    failures: list[str] = []
    table_changes: dict[str, dict[str, int]] = {}
    for table, before_count in sorted(before.table_counts.items()):
        after_count = after.table_counts.get(table, -1)
        if before_count != after_count:
            table_changes[table] = {"before": before_count, "after": after_count}
            failures.append(f"row count changed for {table}: {before_count} -> {after_count}")

    categorical_changes: dict[str, dict[str, dict[str, int]]] = {}
    categories = {
        "payment_status_counts": (before.payment_status_counts, after.payment_status_counts),
        "session_state_counts": (before.session_state_counts, after.session_state_counts),
        "session_protocol_counts": (
            before.session_protocol_counts,
            after.session_protocol_counts,
        ),
        "idempotency_status_counts": (
            before.idempotency_status_counts,
            after.idempotency_status_counts,
        ),
    }
    for name, (before_values, after_values) in categories.items():
        changes = _category_changes(before_values, after_values)
        if changes:
            categorical_changes[name] = changes
            failures.append(f"categorical counts changed for {name}")

    financial_changes: dict[str, dict[str, str]] = {}
    before_financial = before.financial_totals.model_dump()
    after_financial = after.financial_totals.model_dump()
    for name, before_value in before_financial.items():
        after_value = after_financial[name]
        if before_value != after_value:
            financial_changes[name] = {"before": before_value, "after": after_value}
            failures.append(f"financial total changed for {name}: {before_value} -> {after_value}")

    for name, count in after.invariant_violations.model_dump().items():
        if count:
            failures.append(f"post-migration invariant {name} has {count} violation(s)")
    if before.blockers:
        failures.extend(f"pre-migration blocker: {blocker}" for blocker in before.blockers)
    if after.blockers:
        failures.extend(f"post-migration blocker: {blocker}" for blocker in after.blockers)
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
        invariant_violations_after=after.invariant_violations,
        failures=failures,
    )


def _write_json(path: Path, model: BaseModel) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(model.model_dump_json(indent=2) + "\n", encoding="utf-8")


def _read_report(path: Path) -> AuditReport:
    return AuditReport.model_validate_json(path.read_text(encoding="utf-8"))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    snapshot = subparsers.add_parser("snapshot", help="audit DATABASE_URL and write JSON")
    snapshot.add_argument("--phase", choices=("pre", "post"), required=True)
    snapshot.add_argument("--output", type=Path, required=True)

    compare = subparsers.add_parser("compare", help="compare pre/post audit JSON")
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
        _write_json(args.output, report)
        print(
            f"{args.phase} audit: revision={report.alembic_revision} "
            f"tables={len(report.table_counts)} blockers={len(report.blockers)}"
        )
        for blocker in report.blockers:
            print(f"BLOCKER: {blocker}", file=sys.stderr)
        return 1 if report.blockers else 0

    before = _read_report(args.before)
    after = _read_report(args.after)
    comparison = compare_reports(before, after, args.expected_after_revision)
    _write_json(args.output, comparison)
    print(
        f"comparison: {comparison.before_revision} -> {comparison.after_revision}; "
        f"failures={len(comparison.failures)}"
    )
    for failure in comparison.failures:
        print(f"FAIL: {failure}", file=sys.stderr)
    return 1 if comparison.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
