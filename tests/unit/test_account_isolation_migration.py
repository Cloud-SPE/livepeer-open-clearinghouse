"""The account cutover must not silently attribute unresolved legacy money."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


def _migration():  # type: ignore[no-untyped-def]
    path = Path(__file__).parents[2] / "migrations/versions/0029_wholesale_account_namespace.py"
    spec = importlib.util.spec_from_file_location("account_isolation_migration", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(("pending", "active"), [(1, 0), (0, 1), (1, 1)])
def test_cutover_refuses_unresolved_legacy_work(monkeypatch, pending, active):  # type: ignore[no-untyped-def]
    module = _migration()
    counts = iter((pending, active))
    monkeypatch.setattr(
        module,
        "op",
        SimpleNamespace(get_bind=lambda: SimpleNamespace(scalar=lambda _: next(counts))),
    )
    with pytest.raises(RuntimeError, match="Drain and reconcile"):
        module.upgrade()


def test_cutover_preserves_legacy_namespaces_without_relabeling(monkeypatch):  # type: ignore[no-untyped-def]
    module = _migration()
    columns = []
    constraints = []
    monkeypatch.setattr(
        module,
        "op",
        SimpleNamespace(
            get_bind=lambda: SimpleNamespace(scalar=lambda _: 0),
            add_column=lambda table, column: columns.append((table, column)),
            alter_column=lambda *args, **kwargs: None,
            drop_constraint=lambda *args, **kwargs: None,
            create_unique_constraint=lambda *args: constraints.append(args),
        ),
    )
    module.upgrade()
    assert {table for table, _ in columns} == {"wholesale_account", "spend_authorization_grant"}
    for _, column in columns:
        assert column.name == "wholesale_account_id"
        assert column.server_default.arg == ""
    assert "wholesale_account_id" in constraints[0][2]
    with pytest.raises(RuntimeError, match="cannot be merged"):
        module.downgrade()
