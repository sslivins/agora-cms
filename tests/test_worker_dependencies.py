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


def test_dockerfile_worker_copies_every_top_level_package_imported_by_worker() -> None:
    """Every first-party top-level package that ``worker/`` imports at
    module-level must be COPY'd into Dockerfile.worker.

    Background: PR #515-era ``worker/imager_handlers.py`` grew
    ``from cms.services.imager import ...`` at module top, but
    Dockerfile.worker only copied ``shared/`` and ``worker/``.  The
    worker image therefore raised ``ModuleNotFoundError: No module
    named 'cms'`` the first time its handlers were imported.  The
    deploy-time docker import smoke caught it; this test catches it
    at PR time.
    """
    worker_dir = REPO_ROOT / "worker"
    dockerfile = REPO_ROOT / "Dockerfile.worker"
    assert worker_dir.is_dir(), worker_dir
    assert dockerfile.is_file(), dockerfile

    imports = _walk_imports(worker_dir)
    first_party_imported = imports & _first_party_top_levels()

    dockerfile_text = dockerfile.read_text(encoding="utf-8")
    missing: set[str] = set()
    for pkg in first_party_imported:
        # Test packages and alembic are repo-only paths workers never need.
        if pkg in {"tests", "alembic"}:
            continue
        # Look for a COPY line that brings the package directory into the image.
        # Match either "COPY pkg/ pkg/" or "COPY pkg/ <dest>/".
        if f"COPY {pkg}/" not in dockerfile_text:
            missing.add(pkg)

    assert not missing, (
        f"worker/ imports first-party packages {sorted(missing)} at "
        f"module top-level but Dockerfile.worker does not COPY them "
        f"into the image.  The worker container will raise "
        f"ModuleNotFoundError on boot.  Either add ``COPY {{pkg}}/ "
        f"{{pkg}}/`` to Dockerfile.worker, or move the imported "
        f"symbol into ``shared/``."
    )


def test_worker_imports_do_not_pull_in_alembic() -> None:
    """The worker image installs only ``requirements-shared.txt`` and
    ``worker/requirements.txt`` — neither lists ``alembic``.  If any
    module reachable from worker top-level imports does
    ``from alembic import ...`` (or ``import alembic``) at module top,
    the worker container will ``ModuleNotFoundError`` on first use.

    This caught a regression where ``cms/database.py`` used
    ``from alembic import command`` at module top, and the worker
    transitively imported it via ``cms.models.api_key`` →
    ``cms.database`` (because ``cms/models/__init__.py`` aggregates
    every model when *any* ``cms.models.*`` is imported).

    Runs in a subprocess so the import-tracking shim and module-cache
    wipes can't pollute other tests sharing the xdist worker (e.g.
    ``test_logs_response_shim`` relies on ``shared.database`` module
    state that this check would otherwise reset).
    """
    import subprocess
    import sys
    import textwrap

    script = textwrap.dedent(
        """
        import builtins
        import importlib
        import sys

        triggered = []
        real_import = builtins.__import__

        def tracking_import(name, *args, **kwargs):
            if name == "alembic" or name.startswith("alembic."):
                triggered.append(name)
            return real_import(name, *args, **kwargs)

        builtins.__import__ = tracking_import
        try:
            importlib.import_module("worker.imager_handlers")
            importlib.import_module("worker.transcoder")
            importlib.import_module("worker.__main__")
        finally:
            builtins.__import__ = real_import

        if triggered:
            print("ALEMBIC_IMPORTED:" + ",".join(triggered[:5]))
            sys.exit(2)
        sys.exit(0)
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, (
        f"Worker top-level imports pulled in alembic; the worker image "
        f"does not install alembic and will crash on boot.  Move alembic "
        f"imports inside migration functions (see ``cms/database.py``) "
        f"or otherwise break the chain.\n"
        f"stdout: {proc.stdout!r}\nstderr: {proc.stderr!r}"
    )
