#!/usr/bin/env python3
"""Layered-architecture lint for the pymthouse package.

Walks every ``.py`` file under ``src/pymthouse/`` (excluding the
``_gen/`` directory of generated stubs), parses its imports, and
applies the rules from ARCHITECTURE.md:

    1. Composition files (top-level main.py, dependencies.py,
       errors.py, settings.py, __init__.py) are exempt from layering.
    2. Domain file (domains/<D>/<file>.py):
        - same-domain imports must target a layer at or below the
          importing file's layer
        - cross-domain imports may only target the imported domain's
          types/config/repo/service tier (never runtime/ui)
        - provider imports are always OK
    3. Provider file (providers/<P>/...) may NOT import from any
       domains/* module.

The script prints one violation per line in the form
``path:line: rule`` and exits with code 1 if any violations were found.
"""

from __future__ import annotations

import ast
import sys
from collections.abc import Iterator
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "src" / "pymthouse"
PACKAGE_PREFIX = "pymthouse."

LAYERS: list[str] = ["types", "config", "repo", "service", "runtime", "ui"]
SERVICE_TIER_ALIASES: set[str] = {"oauth"}  # service-level siblings of service.py
COMPOSITION_MODULES: set[str] = {
    "main",
    "dependencies",
    "errors",
    "settings",
    "__init__",
}
EXCLUDED_TOP_LEVEL_DIRS: set[str] = {"_gen"}


def layer_index(module_name: str) -> int | None:
    """Return the layer index for a domain-tier filename, or None if not a layer."""
    if module_name in LAYERS:
        return LAYERS.index(module_name)
    if module_name in SERVICE_TIER_ALIASES:
        return LAYERS.index("service")
    return None


def classify(rel_parts: tuple[str, ...]) -> tuple[str, ...]:
    """Map a relative path (under pymthouse/) to a category tuple.

    Returns one of:
        ("composition",)
        ("domain", <domain>, <layer_filename_without_suffix>)
        ("provider", <provider>, <rest_of_path...>)
        ("excluded",)
    """
    if rel_parts and rel_parts[0] in EXCLUDED_TOP_LEVEL_DIRS:
        return ("excluded",)
    if len(rel_parts) == 1:
        name = rel_parts[0].removesuffix(".py")
        if name in COMPOSITION_MODULES:
            return ("composition",)
        return ("composition",)  # any other top-level file is composition
    if rel_parts[0] == "domains" and len(rel_parts) >= 3:
        domain = rel_parts[1]
        leaf = rel_parts[-1].removesuffix(".py")
        return ("domain", domain, leaf)
    if rel_parts[0] == "providers" and len(rel_parts) >= 2:
        return ("provider", rel_parts[1], *rel_parts[2:])
    return ("composition",)  # unrecognized -> treat as composition


def iter_imports(tree: ast.AST) -> Iterator[tuple[str, int]]:
    """Yield (module, lineno) for every `from MODULE import ...` and `import MODULE`."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            yield node.module, node.lineno
        elif isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, node.lineno


def check_file(path: Path) -> list[str]:
    rel = path.relative_to(PACKAGE_ROOT)
    rel_parts = rel.parts
    category = classify(rel_parts)
    if category[0] in ("composition", "excluded"):
        return []

    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:
        return [f"{path}:{exc.lineno}: SyntaxError: {exc.msg}"]

    violations: list[str] = []
    for module, lineno in iter_imports(tree):
        if not module.startswith(PACKAGE_PREFIX) and module != "pymthouse":
            continue
        target_path = module.removeprefix(PACKAGE_PREFIX).split(".")
        target_category = classify(tuple(target_path))

        if target_category[0] in ("composition", "excluded"):
            continue  # any file may import from composition / generated

        if category[0] == "provider":
            if target_category[0] == "domain":
                violations.append(
                    f"{path}:{lineno}: provider must not import from domain"
                    f" ({module})"
                )
            continue  # provider->provider is fine

        # category[0] == "domain"
        _, self_domain, self_leaf = category
        self_layer = layer_index(self_leaf)
        if self_layer is None:
            # An unrecognized file inside a domain (e.g. a future helper). Treat
            # as runtime-tier (most permissive) so we don't reject novel files.
            self_layer = LAYERS.index("runtime")

        if target_category[0] == "provider":
            continue  # domain -> provider is fine
        if target_category[0] != "domain":
            continue

        _, target_domain, target_leaf = target_category
        target_layer = layer_index(target_leaf)

        if self_domain == target_domain:
            if target_layer is None:
                continue
            if target_layer > self_layer:
                violations.append(
                    f"{path}:{lineno}: same-domain layer order violation:"
                    f" {self_leaf}(layer={LAYERS[self_layer]}) -> "
                    f"{target_leaf}(layer={LAYERS[target_layer]})"
                )
        else:
            # cross-domain
            if target_layer is None:
                # importing a non-layer file from another domain — disallow as
                # too permissive
                violations.append(
                    f"{path}:{lineno}: cross-domain non-layer import:"
                    f" {self_domain} -> {target_domain}/{target_leaf}"
                )
                continue
            if target_layer > LAYERS.index("service"):
                violations.append(
                    f"{path}:{lineno}: cross-domain import past service tier:"
                    f" {self_domain} -> {target_domain}.{target_leaf}"
                )

    return violations


def walk_package() -> Iterator[Path]:
    for path in PACKAGE_ROOT.rglob("*.py"):
        rel = path.relative_to(PACKAGE_ROOT)
        if rel.parts and rel.parts[0] in EXCLUDED_TOP_LEVEL_DIRS:
            continue
        yield path


def main(roots: list[Path] | None = None) -> int:
    violations: list[str] = []
    for path in roots or walk_package():
        violations.extend(check_file(path))
    for v in violations:
        print(v, file=sys.stderr)
    if violations:
        print(f"\n{len(violations)} layering violation(s)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
