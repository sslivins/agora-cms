"""Guard: composed-slide widgets may only import worker-available deps.

THE BUG THIS CATCHES
--------------------
Composed-slide widgets (``cms/composed/widgets/*.py``) are executed in
**two** images with **different** dependency sets:

* the **CMS** image installs ``requirements.txt`` (FastAPI, Jinja2, …),
* the **worker** image installs only ``requirements-shared.txt`` +
  Playwright (it renders Chromium thumbnails of every slide).

A widget that imports a package present in the CMS image but *not* in
``requirements-shared.txt`` works fine in the CMS (and in this test
suite, which runs with the full CMS deps) yet blows up at transcode
time in the worker with ``ModuleNotFoundError: No module named '…'``.

That is exactly what happened with ``markupsafe`` (a transitive dep of
Jinja2, so present in the CMS but never declared for the worker): three
widgets did ``from markupsafe import escape`` and silently broke worker
transcode for any slide containing them.

A plain "import every widget" smoke test does **not** catch this,
because the test environment has the CMS deps installed too. So this
test is deliberately a **static AST scan** — it never imports the
modules and never depends on what happens to be installed. It asserts
each widget's top-level imports resolve to either the standard library,
a first-party package, or a third-party package that is declared in
``requirements-shared.txt`` (and therefore guaranteed in the worker).
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

WIDGETS_DIR = Path(__file__).resolve().parent.parent / "cms" / "composed" / "widgets"

# First-party top-level packages (always importable in both images).
_FIRST_PARTY = {"cms", "shared", "worker"}

# Third-party packages that requirements-shared.txt installs, expressed
# as their *import* root names (NOT their pip distribution names). Keep
# this in sync with requirements-shared.txt:
#
#   pydantic>=…              -> pydantic
#   pydantic-settings>=…     -> pydantic_settings
#   sqlalchemy[asyncio]>=…   -> sqlalchemy
#   asyncpg>=…               -> asyncpg
#   httpx>=…                 -> httpx
#   azure-storage-blob>=…    -> azure
#   aiohttp>=…               -> aiohttp
#   segno>=…                 -> segno
#
# A widget importing anything outside this set + the stdlib + first-party
# is a worker-transcode failure waiting to happen.
_WORKER_SHARED = {
    "pydantic",
    "pydantic_settings",
    "sqlalchemy",
    "asyncpg",
    "httpx",
    "azure",
    "aiohttp",
    "segno",
}

_ALLOWED_ROOTS = sys.stdlib_module_names | _FIRST_PARTY | _WORKER_SHARED


def _import_roots(tree: ast.AST) -> set[str]:
    """Top-level import root names used by a module (absolute imports)."""
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".", 1)[0])
        elif isinstance(node, ast.ImportFrom):
            # Relative imports (level > 0) are first-party by definition;
            # node.module is None for ``from . import x``.
            if node.level == 0 and node.module:
                roots.add(node.module.split(".", 1)[0])
    return roots


def _widget_files() -> list[Path]:
    return sorted(
        p
        for p in WIDGETS_DIR.glob("*.py")
        if p.name != "__init__.py"
    )


def test_widgets_directory_is_discovered():
    # Sanity: if the path ever moves, fail loudly rather than vacuously
    # passing because the glob matched nothing.
    files = _widget_files()
    assert files, f"no widget modules found under {WIDGETS_DIR}"


def test_widgets_only_import_worker_available_dependencies():
    violations: dict[str, set[str]] = {}
    for path in _widget_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        bad = {
            root
            for root in _import_roots(tree)
            if root not in _ALLOWED_ROOTS
        }
        if bad:
            violations[path.name] = bad

    assert not violations, (
        "Widget(s) import packages that are NOT guaranteed in the worker "
        "image (requirements-shared.txt). These will raise "
        "ModuleNotFoundError during worker transcode even though the CMS "
        "and this test suite have them installed transitively:\n"
        + "\n".join(
            f"  - {name}: {', '.join(sorted(roots))}"
            for name, roots in sorted(violations.items())
        )
        + "\n\nFix: use a stdlib equivalent (e.g. html.escape instead of "
        "markupsafe.escape), or add the package to requirements-shared.txt "
        "AND to _WORKER_SHARED in this test."
    )
