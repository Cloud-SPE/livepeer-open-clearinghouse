"""Unit tests for scripts/check_layering.

We build a tiny pretend `livepeer_open_clearinghouse` package under tmp_path, point the
script's PACKAGE_ROOT at it, and verify each canonical rule fires.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


@pytest.fixture
def fake_pkg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Materialize a synthetic ``livepeer_open_clearinghouse`` package and re-point the lint at it."""
    root = tmp_path / "src" / "livepeer_open_clearinghouse"
    # Composition files
    _write(root / "__init__.py", "")
    _write(root / "main.py", "")
    _write(root / "dependencies.py", "")
    _write(root / "settings.py", "")
    _write(root / "errors.py", "")
    # Domain skeleton (one domain, all layers exist as empty modules)
    for d in ("alpha", "beta"):
        for f in (
            "__init__.py",
            "types.py",
            "config.py",
            "repo.py",
            "service.py",
            "runtime.py",
            "ui.py",
        ):
            _write(root / "domains" / d / f, "")
    # Service-tier alias on the alpha domain
    _write(root / "domains" / "alpha" / "oauth.py", "")
    # Provider skeleton
    for p in ("clock", "telemetry"):
        _write(root / "providers" / p / "__init__.py", "")
    # Excluded _gen dir (the lint shouldn't traverse it)
    _write(
        root / "_gen" / "junk.py",
        "from livepeer_open_clearinghouse.domains.alpha.runtime import x\n",
    )

    # Reload the lint module against this new package root.
    import scripts.check_layering as cl

    monkeypatch.setattr(cl, "PACKAGE_ROOT", root)
    importlib.invalidate_caches()
    return cl, root


@pytest.mark.unit
def test_clean_tree_has_zero_violations(fake_pkg) -> None:
    cl, _ = fake_pkg
    assert cl.main() == 0


@pytest.mark.unit
def test_same_domain_backward_import_fails(fake_pkg) -> None:
    cl, root = fake_pkg
    # repo imports from service — backward
    _write(
        root / "domains" / "alpha" / "repo.py",
        "from livepeer_open_clearinghouse.domains.alpha.service import x\n",
    )
    rc = cl.main()
    assert rc == 1


@pytest.mark.unit
def test_provider_to_domain_import_fails(fake_pkg) -> None:
    cl, root = fake_pkg
    _write(
        root / "providers" / "clock" / "__init__.py",
        "from livepeer_open_clearinghouse.domains.alpha.types import foo\n",
    )
    rc = cl.main()
    assert rc == 1


@pytest.mark.unit
def test_cross_domain_runtime_import_fails(fake_pkg) -> None:
    cl, root = fake_pkg
    _write(
        root / "domains" / "alpha" / "service.py",
        "from livepeer_open_clearinghouse.domains.beta.runtime import router\n",
    )
    rc = cl.main()
    assert rc == 1


@pytest.mark.unit
def test_cross_domain_service_import_ok(fake_pkg) -> None:
    cl, root = fake_pkg
    _write(
        root / "domains" / "alpha" / "service.py",
        "from livepeer_open_clearinghouse.domains.beta.service import foo\n",
    )
    assert cl.main() == 0


@pytest.mark.unit
def test_cross_domain_repo_import_ok(fake_pkg) -> None:
    cl, root = fake_pkg
    _write(
        root / "domains" / "alpha" / "service.py",
        "from livepeer_open_clearinghouse.domains.beta.repo import Foo\n",
    )
    assert cl.main() == 0


@pytest.mark.unit
def test_oauth_is_service_tier_ok(fake_pkg) -> None:
    cl, root = fake_pkg
    # runtime imports oauth (service-tier sibling of service.py) — forward
    _write(
        root / "domains" / "alpha" / "runtime.py",
        "from livepeer_open_clearinghouse.domains.alpha.oauth import find_user\n",
    )
    assert cl.main() == 0


@pytest.mark.unit
def test_gen_directory_is_excluded(fake_pkg) -> None:
    # The fixture already wrote a forbidden import into _gen/junk.py;
    # a clean run should still pass because the lint skips _gen/.
    cl, _ = fake_pkg
    assert cl.main() == 0


@pytest.mark.unit
def test_composition_can_import_anything(fake_pkg) -> None:
    cl, root = fake_pkg
    _write(
        root / "main.py",
        "from livepeer_open_clearinghouse.domains.alpha.runtime import router\n"
        "from livepeer_open_clearinghouse.domains.beta.repo import Thing\n",
    )
    assert cl.main() == 0
