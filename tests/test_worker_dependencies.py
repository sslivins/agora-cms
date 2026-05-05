"""Regression test: worker image must declare every third-party
import that ``worker/`` and ``shared/`` modules pull in at module
import time.

Background: PR #513-era the worker shipped without ``httpx`` in its
image even though ``shared/services/imager_catalog.py`` (and therefore
``worker/imager_handlers.py``) imported it at module top.  The CMS
image worked because ``cms/requirements.txt`` happened to list httpx,
but the worker (which only installs ``worker/requirements.txt`` →
``-r ../requirements-shared.txt``) crashed with ``ModuleNotFoundError``
the first time a queued ``imager.import`` job tried to dispatch.

This test enforces the invariant: any third-party module name used in
top-level ``import``/``from … import`` statements anywhere under
``shared/`` must be declared in ``requirements-shared.txt`` (or be a
stdlib module).  ``worker/`` is allowed to additionally rely on
``worker/requirements.txt``.

This keeps the bug from reappearing the next time someone adds a
top-level ``import foo`` to a shared module without remembering that
the worker image needs ``foo`` too.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Map of import-name → distribution-name where they differ.  Most
# packages match (httpx → httpx); the long tail does not.
IMPORT_TO_DIST = {
    "azure": "azure-storage-blob",  # also azure-storage-queue; presence of either is fine
    "sqlalchemy": "sqlalchemy",
    "asyncpg": "asyncpg",
    "pydantic": "pydantic",
    "pydantic_settings": "pydantic-settings",
    "aiohttp": "aiohttp",
    "httpx": "httpx",
}


def _stdlib_top_levels() -> set[str]:
    """Best-effort enumeration of stdlib top-level names for the
    interpreter running the tests.  Python 3.10+ ships
    ``sys.stdlib_module_names`` which is exactly what we want."""
    return set(getattr(sys, "stdlib_module_names", ()))


def _first_party_top_levels() -> set[str]:
    """Top-level names that map to repo packages (not deps)."""
    return {"shared", "worker", "cms", "tests", "alembic"}


def _read_requirement_names(path: Path) -> set[str]:
    """Parse a pip requirements file into the set of distribution
    names it pins (lower-cased, stripped of version specifiers)."""
    names: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("-r "):
            continue
        # split on first version operator or whitespace
        for sep in ("[", ">=", "<=", "==", ">", "<", "~=", "!=", " "):
            idx = line.find(sep)
            if idx > 0:
                line = line[:idx]
                break
        names.add(line.lower())
    return names


def _walk_imports(directory: Path) -> set[str]:
    """Collect the top-level module name from every top-level
    ``import``/``from … import`` in every .py under ``directory``.
    Conditional / function-local imports are intentionally ignored
    -- those are the ones it's safe to gate behind extras."""
    seen: set[str] = set()
    for py in directory.rglob("*.py"):
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in tree.body:  # only module-level imports
            if isinstance(node, ast.Import):
                for alias in node.names:
                    seen.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0 and node.module:
                    seen.add(node.module.split(".")[0])
    return seen


def test_shared_top_level_imports_are_in_requirements_shared() -> None:
    """Every third-party top-level import in ``shared/`` must be
    installable from ``requirements-shared.txt``.  This is the file
    the worker image installs from -- gaps here crash the worker at
    boot."""
    shared_dir = REPO_ROOT / "shared"
    assert shared_dir.is_dir(), shared_dir

    imports = _walk_imports(shared_dir)
    third_party = imports - _stdlib_top_levels() - _first_party_top_levels()

    declared = _read_requirement_names(
        REPO_ROOT / "requirements-shared.txt"
    )
    # Translate import names → distribution names where they differ.
    needed = {IMPORT_TO_DIST.get(name, name).lower() for name in third_party}

    # ``azure`` import name maps to multiple azure-* dists; treat any
    # azure-* declaration as satisfying it.
    azure_satisfied = any(name.startswith("azure-") for name in declared)

    missing: set[str] = set()
    for dist in needed:
        if dist == "azure-storage-blob" and azure_satisfied:
            continue
        if dist not in declared:
            missing.add(dist)

    assert not missing, (
        f"shared/ imports {sorted(missing)} at top-level but they are "
        f"not declared in requirements-shared.txt.  The worker image "
        f"installs only requirements-shared.txt (+ worker/requirements.txt) "
        f"and will ModuleNotFoundError on boot.  Add the missing "
        f"package(s) to requirements-shared.txt."
    )


def test_worker_can_import_imager_handlers() -> None:
    """Smoke check: the worker's imager handler module must import
    cleanly under the test environment.  This catches the specific
    httpx-missing regression that bricked production -- the test
    runner uses cms/requirements.txt which superset shared, so a
    fresh ``import worker.imager_handlers`` proves only that the
    test env works.  The strong guard is the requirements check
    above; this test just doubles up.
    """
    import importlib

    mod = importlib.import_module("worker.imager_handlers")
    # Sanity: handler entrypoints exposed.
    assert hasattr(mod, "import_base_image_by_id")
