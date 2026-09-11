from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import pytest


def _load_audit_module() -> ModuleType:
    path = Path(__file__).parents[2] / "scripts" / "postgres_migration_audit.py"
    spec = importlib.util.spec_from_file_location("postgres_migration_audit", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def audit() -> ModuleType:
    return _load_audit_module()


def _report(audit: ModuleType, *, revision: str = "0013", payment_count: int = 2):
    zeros = {
        "ledger_balance_mismatches": 0,
        "negative_balances": 0,
        "invalid_payment_refunds": 0,
        "orphan_payments": 0,
        "orphan_sessions": 0,
        "orphan_settlements": 0,
    }
    financial = {
        "credit_balance_wei": "700",
        "credit_ledger_wei": "700",
        "credit_topup_wei": "1000",
        "payment_funded_wei": "300",
        "payment_expected_wei": "300",
        "payment_reserved_wei": "300",
        "payment_refunded_wei": "0",
        "session_funded_wei": "300",
        "session_billed_wei": "300",
        "settlement_billed_wei": "300",
        "spend_window_spent_wei": "300",
    }
    return audit.AuditReport(
        phase="pre" if revision == "0013" else "post",
        captured_at=datetime.now(UTC),
        database_name="loc",
        server_version="16",
        alembic_revision=revision,
        table_counts={"user": 1, "payment": payment_count, "payment_idempotency_key": 0},
        payment_status_counts={"reconciled": payment_count},
        session_state_counts={"closed": 1},
        session_protocol_counts={"http-reqresp@v0": 1},
        idempotency_status_counts={},
        financial_totals=financial,
        invariant_violations=zeros,
        blockers=[],
    )


@pytest.mark.unit
def test_compare_accepts_lossless_migration(audit: ModuleType) -> None:
    before = _report(audit)
    after = _report(audit, revision="0023")

    comparison = audit.compare_reports(before, after, "0023")

    assert comparison.before_revision == "0013"
    assert comparison.after_revision == "0023"
    assert comparison.failures == []
    assert comparison.table_count_changes == {}
    assert comparison.categorical_changes == {}
    assert comparison.financial_changes == {}


@pytest.mark.unit
def test_compare_rejects_row_loss_and_financial_drift(audit: ModuleType) -> None:
    before = _report(audit)
    after = _report(audit, revision="0023", payment_count=1)
    after.payment_status_counts = before.payment_status_counts
    after.financial_totals.credit_balance_wei = "699"

    comparison = audit.compare_reports(before, after, "0023")

    assert comparison.table_count_changes == {"payment": {"before": 2, "after": 1}}
    assert comparison.financial_changes == {"credit_balance_wei": {"before": "700", "after": "699"}}
    assert len(comparison.failures) == 2


@pytest.mark.unit
def test_compare_rejects_preflight_blockers(audit: ModuleType) -> None:
    before = _report(audit)
    before.blockers = ["one v1 session is open"]
    after = _report(audit, revision="0023")

    comparison = audit.compare_reports(before, after, "0023")

    assert comparison.failures == ["pre-migration blocker: one v1 session is open"]


@pytest.mark.unit
def test_compare_rejects_wrong_head_and_changed_statuses(audit: ModuleType) -> None:
    before = _report(audit)
    after = _report(audit, revision="0022")
    after.payment_status_counts = {"issued": 1, "reconciled": 1}

    comparison = audit.compare_reports(before, after, "0023")

    assert comparison.categorical_changes["payment_status_counts"] == {
        "issued": {"before": 0, "after": 1},
        "reconciled": {"before": 2, "after": 1},
    }
    assert comparison.failures == [
        "categorical counts changed for payment_status_counts",
        "expected post-migration revision 0023, found 0022",
    ]
