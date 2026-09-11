"""Run the revision-bound LOC + Modules wholesale rollout gate."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class Case:
    name: str
    requirement: str
    cwd: Path
    command: tuple[str, ...]


def _git_state(repo: Path) -> dict[str, Any]:
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git is required")
    revision = subprocess.run(  # noqa: S603 — fixed diagnostic command
        [git, "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    dirty = subprocess.run(  # noqa: S603 — fixed diagnostic command
        [git, "status", "--porcelain"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return {"revision": revision, "dirty": bool(dirty)}


def _pytest_case(repo: Path, name: str, requirement: str, *node_ids: str) -> Case:
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("uv is required")
    return Case(name, requirement, repo, (uv, "run", "pytest", "-q", *node_ids))


def _cases(repo: Path, modules: Path) -> list[Case]:
    go = shutil.which("go")
    if go is None:
        raise RuntimeError("go is required")
    return [
        _pytest_case(
            repo,
            "intrinsic_authorization_fail_closed",
            "Paid protocol selection intrinsically requires authorization and complete caller proof.",
            "tests/unit/test_discovery_extra.py::test_freeform_features_are_preserved_but_not_protocol_authority",
            "tests/unit/test_payment_session_model.py::test_authorization_issuance_requires_caller_proof_without_feature_negotiation",
        ),
        _pytest_case(
            repo,
            "customer_wholesale_isolation",
            "No customer owns pooled credit and retail pricing is independent of wholesale EV.",
            "tests/unit/test_wholesale_accounting.py::test_wholesale_tables_have_no_customer_ownership_columns",
            "tests/unit/test_wholesale_accounting.py::test_customer_ledger_correlates_to_engagement_without_payment",
            "tests/unit/test_wholesale_accounting.py::test_customer_charge_policy_is_independent_of_ticket_ev",
        ),
        _pytest_case(
            repo,
            "bounded_shortfall_and_replay",
            "Aggregate funding mints only bounded shortfall and replay cannot remint or double-count exposure.",
            "tests/unit/test_wholesale_accounting.py::test_shortfall_plan_and_request_use_aggregate_account_not_customer_maximum",
            "tests/unit/test_wholesale_accounting.py::test_shortfall_plan_mints_nothing_at_or_above_target",
            "tests/unit/test_wholesale_accounting.py::test_shortfall_plan_fails_closed_at_operator_limits",
            "tests/unit/test_wholesale_accounting.py::test_funding_claim_mint_and_ack_are_durable",
            "tests/unit/test_wholesale_accounting.py::test_minted_funding_replays_persisted_bytes_without_reminting",
        ),
        _pytest_case(
            repo,
            "authorization_scope_revision_and_cap",
            "Authorizations are route/workload bound, append-only by revision, and capped independently of runway.",
            "tests/unit/test_payment_session_model.py::test_authorization_revisions_preserve_original_grant",
            "tests/unit/test_open_session_service.py::test_open_session_wholesale_uses_cumulative_authorization_and_shared_runway",
            "tests/unit/test_jobs_service.py::test_open_job_wholesale_returns_authorization_not_pool_ticket",
            "tests/unit/test_settlement_verification.py::test_wholesale_job_settlement_is_bound_to_authorization_and_ceiling",
        ),
        _pytest_case(
            repo,
            "sdk_independent_reconciliation",
            "Durable broker evidence settles by LOC request ID without a customer or SDK callback.",
            "tests/unit/test_jobs_service.py::test_reconcile_open_job_settles_only_embedded_signed_claim",
            "tests/unit/test_jobs_service.py::test_reconcile_open_job_rejects_cross_request_signed_settlement",
            "tests/unit/test_broker_settlement.py::test_spend_authorization_query_is_strict_and_identity_bound",
        ),
        _pytest_case(
            repo,
            "loc_retry_crash_and_concurrency",
            "Request retries, lost responses, crashes, and concurrent creates produce one durable economic mutation.",
            "tests/unit/test_wholesale_accounting.py::test_funding_claim_mint_and_ack_are_durable",
            "tests/unit/test_wholesale_accounting.py::test_minted_funding_replays_persisted_bytes_without_reminting",
            "tests/unit/test_create_idempotency.py::test_concurrent_stale_recovery_has_one_winner_and_stable_id",
        ),
        Case(
            "modules_payer_receiver_contract",
            "The real Go payer/receiver implementations enforce scoped idempotent authorization and shortfall funding.",
            modules / "payment-daemon",
            (go, "test", "./internal/service/sender", "./internal/service/receiver"),
        ),
        Case(
            "modules_broker_account_contract",
            "The real Go broker prevents altered-workload replay, double debit, and full-cap session reservation.",
            modules / "capability-broker",
            (
                go,
                "test",
                "./internal/server/...",
                "./internal/sessionengine",
            ),
        ),
        _pytest_case(
            repo,
            "wholesale_only_cutover",
            "The schema blocks active legacy work and every request boundary requires wholesale authorization inputs.",
            "tests/unit/test_schema_revision.py::test_wholesale_cutover_migration_blocks_active_legacy_engagements",
            "tests/unit/test_wholesale_request_contract.py",
        ),
    ]


def _run_case(case: Case, artifacts: Path) -> dict[str, Any]:
    started = time.monotonic()
    completed = subprocess.run(  # noqa: S603 — matrix contains only repository-owned commands
        list(case.command),
        cwd=case.cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    log = completed.stdout + completed.stderr
    log_path = artifacts / f"{case.name}.log"
    log_path.write_text(log, encoding="utf-8")
    return {
        "name": case.name,
        "requirement": case.requirement,
        "status": "passed" if completed.returncode == 0 else "failed",
        "returncode": completed.returncode,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "command": list(case.command),
        "log": log_path.name,
        "log_sha256": hashlib.sha256(log.encode()).hexdigest(),
    }


def run(repo: Path, modules: Path, artifacts: Path, *, live_stack: bool) -> dict[str, Any]:
    repo = repo.resolve()
    modules = modules.resolve()
    loc_state = _git_state(repo)
    modules_state = _git_state(modules)
    if loc_state["dirty"] or modules_state["dirty"]:
        raise RuntimeError("immutable evidence requires clean LOC and Modules worktrees")
    artifacts.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    for case in _cases(repo, modules):
        result = _run_case(case, artifacts)
        results.append(result)
        if result["status"] != "passed":
            break
    if live_stack and all(result["status"] == "passed" for result in results):
        uv = shutil.which("uv")
        if uv is None:
            raise RuntimeError("uv is required")
        results.append(
            _run_case(
                Case(
                    "real_process_wholesale_control",
                    "Pinned real LOC and Modules processes prove wholesale-only SDK and raw-HTTP behavior.",
                    repo,
                    (
                        uv,
                        "run",
                        "python",
                        "conformance/live/stack_harness.py",
                        f"--modules-repo={modules}",
                        f"--artifacts={artifacts / 'live-stack'}",
                    ),
                ),
                artifacts,
            )
        )
    report = {
        "format_version": 1,
        "captured_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "status": "passed" if all(item["status"] == "passed" for item in results) else "failed",
        "loc": loc_state,
        "modules": modules_state,
        "live_stack_required": live_stack,
        "cases": results,
    }
    (artifacts / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main() -> int:
    repo = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=repo)
    parser.add_argument(
        "--modules-repo", type=Path, default=repo.parent / "livepeer-network-modules"
    )
    parser.add_argument(
        "--artifacts", type=Path, default=Path(".artifacts/live-conformance/wholesale")
    )
    parser.add_argument("--skip-live-stack", action="store_true")
    args = parser.parse_args()
    try:
        report = run(
            args.repo,
            args.modules_repo,
            args.artifacts,
            live_stack=not args.skip_live_stack,
        )
    except Exception as exc:
        print(f"wholesale rollout gate failed: {exc}")
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
