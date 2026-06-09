"""Tests for the worker transcode-failure metric (issue #474, Layer 2).

Two layers of coverage:

1. **Registry contract** (mirrors ``tests/test_metrics.py`` for
   ``cms.metrics``): ``shared.metrics`` imports cleanly even with no
   telemetry SDK configured, exposes the ``transcode_failure_total``
   counter handle, pins the bounded ``reason`` value-set, and
   ``.add()`` with attributes is always safe (a no-op under the
   default no-op ``MeterProvider``).

2. **Wiring guard** (static analysis of ``worker/__main__.py``, mirrors
   ``tests/test_worker_observability_wiring.py``): the worker imports
   the registry from ``shared`` (never ``cms.*`` — unimportable in the
   worker image) and increments ``transcode_failure_total`` at each of
   the three genuine-failure finalize branches.  This stops a refactor
   from silently dropping the increments — which would re-create the
   exact "failures only surface in worker stdout" gap that motivated
   the feature.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_WORKER_MAIN = Path(__file__).resolve().parent.parent / "worker" / "__main__.py"


# ----------------------------------------------------------------------
# Registry contract
# ----------------------------------------------------------------------


def test_module_imports_without_telemetry_configured() -> None:
    # Cold-import smoke in a subprocess (see test_metrics.py for the
    # rationale — avoid reload()/identity pitfalls and exercise the
    # real cold path).  shared.metrics is imported at module-load by
    # worker/__main__.py; an import-time throw breaks the worker.
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-c", "import shared.metrics"],
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"cold-import of shared.metrics failed:\n"
        f"stdout={result.stdout.decode()!r}\n"
        f"stderr={result.stderr.decode()!r}"
    )


def test_transcode_failure_counter_handle_exposed() -> None:
    from shared import metrics

    handle = metrics.transcode_failure_total
    assert hasattr(handle, "add"), (
        "transcode_failure_total should expose an OTel-style .add() method"
    )


def test_attribute_key_constants() -> None:
    from shared import metrics

    assert metrics.ATTR_REASON == "reason"
    assert metrics.ATTR_JOB_TYPE == "job_type"


def test_failure_reason_constants_are_bounded() -> None:
    from shared import metrics

    # Bounded value set for the ``reason`` attribute — every distinct
    # value is a separate series in App Insights, so keep this short.
    # Pin it so a future PR adding a new reason must update this test
    # (and any workbook KQL / metric alert filtering on reason).
    assert {
        metrics.REASON_IMPORT_ERROR,
        metrics.REASON_TIMEOUT,
        metrics.REASON_IMAGER_TERMINAL,
        metrics.REASON_RENDER_ERROR,
        metrics.REASON_UNKNOWN,
    } == {
        "import_error",
        "timeout",
        "imager_terminal",
        "render_error",
        "unknown",
    }


@pytest.mark.parametrize(
    "reason_attr",
    [
        "REASON_IMPORT_ERROR",
        "REASON_TIMEOUT",
        "REASON_IMAGER_TERMINAL",
        "REASON_RENDER_ERROR",
        "REASON_UNKNOWN",
    ],
)
def test_add_with_reason_and_job_type_is_safe(reason_attr: str) -> None:
    from shared import metrics

    # Under the default no-op MeterProvider this is a no-op; under a
    # real SDK it records.  Either way it must not raise.
    metrics.transcode_failure_total.add(
        1,
        {
            metrics.ATTR_REASON: getattr(metrics, reason_attr),
            metrics.ATTR_JOB_TYPE: "variant_transcode",
        },
    )


# ----------------------------------------------------------------------
# Wiring guard (static analysis of worker/__main__.py)
# ----------------------------------------------------------------------


def _module() -> ast.Module:
    return ast.parse(_WORKER_MAIN.read_text(encoding="utf-8"))


def test_worker_imports_metrics_from_shared() -> None:
    tree = _module()
    imported_from_shared = any(
        isinstance(node, ast.ImportFrom)
        and node.module == "shared"
        and any(alias.name == "metrics" for alias in node.names)
        for node in ast.walk(tree)
    )
    assert imported_from_shared, (
        "worker/__main__.py must import `metrics` from `shared` "
        "(the worker image cannot import cms.*)"
    )
    # Guard against accidentally importing the CMS registry, which is
    # unimportable in the worker image and would crash at startup.
    cms_import = any(
        isinstance(node, ast.ImportFrom)
        and (node.module or "").startswith("cms")
        and any(alias.name == "metrics" for alias in node.names)
        for node in ast.walk(tree)
    )
    assert not cms_import, "metrics must not be imported from cms.*"


def _count_failure_increments(tree: ast.Module) -> int:
    """Count ``metrics.transcode_failure_total.add(...)`` call sites."""
    count = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # metrics.transcode_failure_total.add(...)
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "add"
            and isinstance(func.value, ast.Attribute)
            and func.value.attr == "transcode_failure_total"
            and isinstance(func.value.value, ast.Name)
            and func.value.value.id == "metrics"
        ):
            count += 1
    return count


def test_worker_increments_counter_at_each_failure_branch() -> None:
    tree = _module()
    # Three genuine-failure finalize branches increment the counter:
    # SIGTERM timeout, TerminalImagerError, and the generic
    # exception / handler-returned-False retry branch.  Lease loss,
    # cancellation, and success deliberately do NOT.
    assert _count_failure_increments(tree) == 3, (
        "worker/__main__.py must increment metrics.transcode_failure_total "
        "at exactly the three genuine-failure finalize branches "
        "(timeout, imager_terminal, failure/retry)"
    )
