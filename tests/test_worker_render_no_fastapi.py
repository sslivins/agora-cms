"""Regression guard: the worker thumbnail-render import chain must NOT
require ``fastapi``.

``fastapi`` is a CMS-only dependency and is intentionally absent from the
worker image (``Dockerfile.worker`` / ``worker/requirements.txt``). The
worker renders composed-slide thumbnails by importing
``cms.composed.render.build_composed_html`` and ``worker.composed_render``.

This has regressed twice: first via ``cms.composed.render`` itself
(fixed in PR #728 with a guarded import), then via
``cms.services.asset_readiness`` after the slideshow-in-composed feature
added ``render -> slideshow_expand -> slideshow_resolver -> asset_readiness``
to the chain. Both surfaced in production as
``No module named 'fastapi'`` thumbnail-transcode failures.

This test runs in a subprocess with ``fastapi`` blocked from import so a
new unguarded top-level ``from fastapi import ...`` anywhere on the worker
render chain fails CI instead of silently breaking every composed
thumbnail in the field.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap


def test_worker_render_chain_imports_without_fastapi() -> None:
    script = textwrap.dedent(
        """
        import builtins, sys
        _real = builtins.__import__
        def _blocked(name, *a, **k):
            if name.split('.')[0] == 'fastapi':
                raise ModuleNotFoundError("No module named 'fastapi'")
            return _real(name, *a, **k)
        builtins.__import__ = _blocked
        for m in list(sys.modules):
            if m.split('.')[0] == 'fastapi':
                del sys.modules[m]

        # The exact symbols the worker imports to render thumbnails.
        from cms.composed.render import build_composed_html  # noqa: F401
        from worker.composed_render import render_composed_to_png  # noqa: F401
        print("OK")
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (
        "Worker render import chain pulled in fastapi (CMS-only dep, not in "
        "the worker image). A module on the chain has an unguarded "
        "`from fastapi import ...`.\n\nSTDOUT:\n"
        f"{proc.stdout}\n\nSTDERR:\n{proc.stderr}"
    )
    assert "OK" in proc.stdout
