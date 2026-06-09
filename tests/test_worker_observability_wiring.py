"""Regression guard: the transcode worker MUST wire up Application
Insights / OpenTelemetry at startup.

Background (issue #474): ``setup_observability()`` is what hands the
process to ``azure-monitor-opentelemetry`` so unhandled exceptions are
exported to the App Insights ``exceptions`` table.  The CMS process
calls it from ``cms/main.py``; for a long time the **worker** never
did, so transcode failures (e.g. the ``markupsafe`` worker-import bug)
died silently in worker stdout and never surfaced in telemetry.

This test enforces the invariant by static analysis so it can't be
re-broken by a refactor that drops the call:

* ``worker/__main__.py`` imports ``setup_observability`` from
  ``shared.observability`` (NOT ``cms.observability`` — the worker
  image cannot import ``cms.*``).
* ``async def main()`` invokes ``setup_observability(...)`` as its
  FIRST statement, passing ``role_name="agora-worker"`` so worker
  exceptions are tagged with a distinct ``cloud_RoleName``.
"""
from __future__ import annotations

import ast
from pathlib import Path

_WORKER_MAIN = Path(__file__).resolve().parent.parent / "worker" / "__main__.py"


def _module() -> ast.Module:
    return ast.parse(_WORKER_MAIN.read_text(encoding="utf-8"))


def test_worker_imports_setup_observability_from_shared() -> None:
    tree = _module()
    imported_from_shared = any(
        isinstance(node, ast.ImportFrom)
        and node.module == "shared.observability"
        and any(alias.name == "setup_observability" for alias in node.names)
        for node in ast.walk(tree)
    )
    assert imported_from_shared, (
        "worker/__main__.py must import setup_observability from "
        "shared.observability (the worker image cannot import cms.*)"
    )
    # Guard against accidentally importing it from cms.* (unimportable
    # in the worker image and would crash at startup).
    cms_import = any(
        isinstance(node, ast.ImportFrom)
        and (node.module or "").startswith("cms.")
        and any(alias.name == "setup_observability" for alias in node.names)
        for node in ast.walk(tree)
    )
    assert not cms_import, "setup_observability must not be imported from cms.*"


def test_main_calls_setup_observability_first_with_worker_role() -> None:
    tree = _module()
    main_fn = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "main"
        ),
        None,
    )
    assert main_fn is not None, "worker/__main__.py must define `async def main()`"

    # First *statement* of main() must be the setup_observability call.
    first_stmt = main_fn.body[0]
    assert isinstance(first_stmt, ast.Expr) and isinstance(
        first_stmt.value, ast.Call
    ), "setup_observability(...) must be the first statement in main()"

    call = first_stmt.value
    assert (
        isinstance(call.func, ast.Name) and call.func.id == "setup_observability"
    ), "first statement of main() must call setup_observability(...)"

    role = next(
        (kw.value for kw in call.keywords if kw.arg == "role_name"), None
    )
    assert (
        isinstance(role, ast.Constant) and role.value == "agora-worker"
    ), 'worker must call setup_observability(role_name="agora-worker")'
