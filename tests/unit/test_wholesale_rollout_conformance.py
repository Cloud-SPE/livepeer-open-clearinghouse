from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest


def _load() -> ModuleType:
    path = Path(__file__).parents[2] / "conformance" / "wholesale_rollout.py"
    spec = importlib.util.spec_from_file_location("wholesale_rollout", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def runner() -> ModuleType:
    return _load()


@pytest.mark.unit
def test_matrix_names_every_required_rollout_invariant(runner: ModuleType, tmp_path: Path) -> None:
    names = {case.name for case in runner._cases(tmp_path, tmp_path)}

    assert names == {
        "negotiation_fail_closed",
        "customer_wholesale_isolation",
        "bounded_shortfall_and_replay",
        "authorization_scope_revision_and_cap",
        "sdk_independent_reconciliation",
        "loc_retry_crash_and_concurrency",
        "modules_payer_receiver_contract",
        "modules_broker_account_contract",
        "legacy_regression",
    }


@pytest.mark.unit
def test_case_result_is_machine_readable_and_log_is_hashed(
    runner: ModuleType, tmp_path: Path
) -> None:
    case = runner.Case(
        name="probe",
        requirement="The runner records exact evidence.",
        cwd=tmp_path,
        command=(sys.executable, "-c", "print('passed')"),
    )

    result = runner._run_case(case, tmp_path)

    assert result["status"] == "passed"
    assert result["returncode"] == 0
    assert result["log"] == "probe.log"
    assert len(result["log_sha256"]) == 64
    assert (tmp_path / "probe.log").read_text() == "passed\n"


@pytest.mark.unit
def test_case_failure_blocks_the_matrix(runner: ModuleType, tmp_path: Path) -> None:
    case = runner.Case(
        name="probe",
        requirement="Failures block release.",
        cwd=tmp_path,
        command=(sys.executable, "-c", "raise SystemExit(7)"),
    )

    result = runner._run_case(case, tmp_path)

    assert result["status"] == "failed"
    assert result["returncode"] == 7
