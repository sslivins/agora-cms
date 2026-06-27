"""Regression guard: the worker thumbnail-render import chain must NOT
require any CMS-only web-framework dependency.

The worker image (``Dockerfile.worker`` / ``worker/requirements.txt``)
installs only ``requirements-shared.txt`` + the worker's own deps. It does
**not** install the CMS web stack — ``fastapi``, ``starlette``,
``uvicorn`` — which live exclusively in ``requirements.txt``.

The worker renders composed-slide thumbnails by importing
``cms.composed.render.build_composed_html`` and
``worker.composed_render.render_composed_to_png`` (see
``worker/transcoder.py::_render_composed_thumbnail``). If *any* module on
that transitive import chain gains an unguarded top-level
``from fastapi import ...`` (or starlette/uvicorn), the worker raises
``No module named 'fastapi'`` at render time and **every** composed
thumbnail transcode fails — silently, since it only shows up in worker
logs.

This bug class has regressed three times:

* PR #728 — ``cms.composed.render`` imported fastapi directly.
* PR #758 — ``markupsafe`` (jinja2) crept onto the worker chain.
* The fastapi import re-appeared via
  ``cms.services.asset_readiness`` once the slideshow-in-composed feature
  added ``render -> slideshow_expand -> slideshow_resolver ->
  asset_readiness`` to the chain. It broke every composed thumbnail for
  ~17 days before anyone noticed.

This test runs in a subprocess with the whole web-framework set blocked
from import, so a new unguarded import anywhere on the worker render
chain fails CI with a traceback pointing at the exact offending line —
reproducing the worker crash instead of letting it ship silently.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

# CMS-only web-framework roots that the worker image never installs. Any
# of these appearing on the worker render chain means a broken thumbnail
# transcode in the field.
FORBIDDEN_ROOTS = ("fastapi", "starlette", "uvicorn")

# The exact symbols/modules the worker imports to render a composed
# thumbnail. Keep in sync with ``worker/transcoder.py``.
WORKER_RENDER_IMPORTS = (
    "import worker.transcoder",
    "from cms.composed.render import build_composed_html",
    "from worker.composed_render import render_composed_to_png",
)


def test_worker_render_chain_imports_without_web_framework() -> None:
    blocked = ", ".join(repr(r) for r in FORBIDDEN_ROOTS)
    imports = "\n        ".join(WORKER_RENDER_IMPORTS)
    script = textwrap.dedent(
        f"""
        import builtins, sys
        _blocked_roots = {{{blocked}}}
        _real = builtins.__import__
        def _blocked(name, *a, **k):
            root = name.split('.')[0]
            if root in _blocked_roots:
                raise ModuleNotFoundError("No module named '" + root + "'")
            return _real(name, *a, **k)
        builtins.__import__ = _blocked
        for m in list(sys.modules):
            if m.split('.')[0] in _blocked_roots:
                del sys.modules[m]

        {imports}
        print("OK")
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (
        "The worker render import chain pulled in a CMS-only web framework "
        f"({', '.join(FORBIDDEN_ROOTS)}) — none of which are in the worker "
        "image. A module on the chain has an unguarded top-level import "
        "(e.g. `from fastapi import ...`). Guard it lazily so the worker can "
        "render composed thumbnails without the CMS web stack.\n\n"
        f"STDOUT:\n{proc.stdout}\n\nSTDERR:\n{proc.stderr}"
    )
    assert "OK" in proc.stdout
