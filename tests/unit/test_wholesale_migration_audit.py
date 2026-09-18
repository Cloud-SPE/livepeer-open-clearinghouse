from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import pytest


def _load() -> ModuleType:
    path = Path(__file__).parents[2] / "scripts" / "wholesale_migration_audit.py"
    spec = importlib.util.spec_from_file_location("wholesale_migration_audit", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def audit() -> ModuleType:
    return _load()


def _report(audit: ModuleType, *, revision: str, phase: str = "pre"):
    return audit.AuditReport(
        phase=phase,
        captured_at=datetime.now(UTC),
        database_name="loc",
        server_version="16",
        alembic_revision=revision,
        table_counts={"payment": 4, "payment_session": 2, "credit_ledger": 6},
        payment_status_counts={"reconciled": 4},
        session_state_counts={"closed": 2},
        session_protocol_counts={"http-reqresp@v0": 1, "segment-streaming@v0": 1},
        financial_totals={
            "credit_balance_wei": "700",
            "credit_ledger_wei": "700",
            "payment_funded_wei": "300",
            "payment_reserved_wei": "300",
            "payment_refunded_wei": "0",
            "session_funded_wei": "300",
            "session_billed_wei": "200",
            "settlement_billed_wei": "200",
        },
        wholesale_initialization=(
            {
                "global_budget_rows": 1,
                "global_projected_available_wei": "0",
            }
            if phase == "post"
            else None
        ),
        blockers=[],
    )


@pytest.mark.unit
def test_compare_accepts_lossless_disabled_wholesale_migration(audit: ModuleType) -> None:
    before = _report(audit, revision="0024")
    after = _report(audit, revision="0026", phase="post")

    result = audit.compare_reports(before, after, "0026")

    assert result.failures == []
    assert result.table_count_changes == {}
    assert result.categorical_changes == {}
    assert result.financial_changes == {}


@pytest.mark.unit
def test_compare_rejects_reclassification_and_financial_drift(audit: ModuleType) -> None:
    before = _report(audit, revision="0024")
    after = _report(audit, revision="0026", phase="post")
    after.session_state_counts = {"closed": 1, "open": 1}
    after.financial_totals.credit_balance_wei = "699"
    after.blockers = ["wholesale initialization non_legacy_sessions is 1, expected zero"]

    result = audit.compare_reports(before, after, "0026")

    assert result.categorical_changes["session_state_counts"] == {
        "closed": {"before": 2, "after": 1},
        "open": {"before": 0, "after": 1},
    }
    assert result.financial_changes["credit_balance_wei"] == {
        "before": "700",
        "after": "699",
    }
    assert any("non_legacy_sessions" in failure for failure in result.failures)


@pytest.mark.unit
def test_compare_rejects_drain_blocker_row_loss_and_wrong_revision(audit: ModuleType) -> None:
    before = _report(audit, revision="0024")
    before.blockers = ["one legacy session is open or draining"]
    after = _report(audit, revision="0025", phase="post")
    after.table_counts["payment"] = 3

    result = audit.compare_reports(before, after, "0026")

    assert "pre-migration blocker: one legacy session is open or draining" in result.failures
    assert "row count changed for payment: 4 -> 3" in result.failures
    assert "expected post-migration revision 0026, found 0025" in result.failures
