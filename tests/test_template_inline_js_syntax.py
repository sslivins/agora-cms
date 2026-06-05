"""Regression guard: every inline ``<script>`` block in a Jinja template
must be syntactically valid JavaScript.

Why this exists
---------------
PR #714 dropped a single ``panel.innerHTML = `` opener inside
``composed_editor.html``'s ``renderSettings``. That turned the rest of
the settings template literal into bare ``${...}`` expressions sitting
outside any string, so the whole ``<script>`` failed to parse with
``Uncaught SyntaxError: Unexpected token '{'``. Because the script never
ran, *every* handler on the page died -- widget selection AND the
unsaved-changes guard -- yet CI stayed green:

* the fast Python ``test`` job renders templates only as strings and
  never executes their JS, and
* the nightly Playwright "Smoke Test" never navigates to the composed
  editor and attaches no ``pageerror`` listener.

So a pure-JS syntax error in any inline ``<script>`` sailed through.
This test closes that gap cheaply: it extracts each template's inline
script, neutralises the Jinja tags, and runs ``node --check`` on the
result. ``node`` understands modern JS (optional ``catch {}``, optional
chaining, template literals) so it is the correct parser for the whole
template set -- a pure-Python ES2017 parser (esprima/pyjsparser) chokes
on the ES2019/ES2020 syntax these templates legitimately use.

Jinja neutralisation
--------------------
The script text is NOT a finished JS file -- it still has ``{{ ... }}``
and ``{% ... %}`` in it. We strip those with a small regex resolver:

* ``{% if %}A{% else %}B{% endif %}`` -> keep the first branch ``A``
* ``{% if %}A{% endif %}``            -> keep ``A``
* any other ``{% ... %}``             -> drop
* ``{{ expr }}``                      -> literal ``0``

This is approximate, not a full Jinja engine. The known blind spot is an
*inline* either/or that produces two different JS tokens, e.g.
``{% if x %}'a'{% else %}'b'{% endif %}`` -- keeping the first branch is
fine, but a construct that is only valid when the *second* branch is
chosen could in theory false-positive. No current template does this; if
one ever trips the guard for that reason, refine the resolver rather than
deleting the test.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES_DIR = REPO_ROOT / "cms" / "templates"

# Only bare ``<script>`` (no attributes) carries inline page JS. Tags with
# ``src=...`` have no body and ``type="application/json"`` blocks are data,
# not JS -- both are naturally excluded by requiring a bare opening tag.
_SCRIPT_RE = re.compile(r"<script>(.*?)</script>", re.S)


def _neutralize_jinja(js: str) -> str:
    """Turn a Jinja-laced inline script into parseable JS (see module docstring)."""
    js = re.sub(
        r"\{%\s*if.*?%\}(.*?)\{%\s*else\s*%\}.*?\{%\s*endif\s*%\}",
        r"\1",
        js,
        flags=re.S,
    )
    js = re.sub(r"\{%\s*if.*?%\}(.*?)\{%\s*endif\s*%\}", r"\1", js, flags=re.S)
    js = re.sub(r"\{%.*?%\}", "", js, flags=re.S)
    js = re.sub(r"\{\{.*?\}\}", "0", js, flags=re.S)
    return js


def _inline_scripts(html: str) -> list[str]:
    return _SCRIPT_RE.findall(html)


def _templates_with_inline_js() -> list[Path]:
    out: list[Path] = []
    for path in sorted(TEMPLATES_DIR.rglob("*.html")):
        if _inline_scripts(path.read_text(encoding="utf-8")):
            out.append(path)
    return out


_NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(
    _NODE is None,
    reason="node not on PATH; inline-JS syntax guard requires Node (present in CI).",
)


def _node_check(js: str) -> tuple[int, str]:
    """Run ``node --check`` on a JS string. Returns (returncode, stderr)."""
    with tempfile.NamedTemporaryFile(
        "w", suffix=".js", delete=False, encoding="utf-8"
    ) as fh:
        fh.write(js)
        tmp = fh.name
    try:
        proc = subprocess.run(
            [_NODE, "--check", tmp],
            capture_output=True,
            text=True,
        )
        return proc.returncode, proc.stderr
    finally:
        Path(tmp).unlink(missing_ok=True)


_TEMPLATES = _templates_with_inline_js()


def test_templates_with_inline_js_were_discovered() -> None:
    """Fail loudly if the glob/regex stops finding any inline scripts.

    Guards against a future refactor (e.g. templates moved, or the
    ``<script>`` regex going stale) silently turning the parametrized
    guard below into a no-op that collects zero cases and "passes".
    """
    assert _TEMPLATES, (
        "No templates with inline <script> blocks were found under "
        f"{TEMPLATES_DIR}. The inline-JS syntax guard would be a no-op -- "
        "check the template path or the <script> regex."
    )


@pytest.mark.parametrize(
    "template",
    _TEMPLATES,
    ids=[str(p.relative_to(TEMPLATES_DIR)) for p in _TEMPLATES],
)
def test_inline_script_is_valid_js(template: Path) -> None:
    """Each inline ``<script>`` block parses as valid JavaScript."""
    html = template.read_text(encoding="utf-8")
    # Check each block independently: separate <script> tags are separate
    # top-level programs, so concatenating them could raise a spurious
    # "Identifier already declared" across blocks that is fine in the page.
    for idx, block in enumerate(_inline_scripts(html)):
        js = _neutralize_jinja(block)
        rc, stderr = _node_check(js)
        assert rc == 0, (
            f"{template.relative_to(REPO_ROOT)} inline <script> #{idx + 1} "
            f"failed to parse as JavaScript:\n{stderr.strip()}"
        )
