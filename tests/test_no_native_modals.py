"""
Static lint: forbid native confirm()/prompt()/alert() in the web UI.

The CMS provides custom modal helpers (showConfirm/showPrompt/showToast in
cms/static/app.js) that match the rest of the UI. Using the browser's
native dialogs is jarring, breaks E2E tests that auto-dismiss them, and
violates the convention documented in .github/copilot-instructions.md.

This test scans cms/templates/**/*.html and cms/static/**/*.js, strips
JS line/block comments, Jinja comments ({# #}), and HTML comments
(<!-- -->), and fails if any bare confirm(/prompt(/alert( call remains.
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCAN_ROOTS = [REPO_ROOT / "cms" / "templates", REPO_ROOT / "cms" / "static"]

NATIVE_MODAL = re.compile(r"\b(?:confirm|prompt|alert)\s*\(")
LINE_COMMENT = re.compile(r"//.*$", re.MULTILINE)
BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.DOTALL)
HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


def _strip_comments(text: str) -> str:
    text = BLOCK_COMMENT.sub("", text)
    text = JINJA_COMMENT.sub("", text)
    text = HTML_COMMENT.sub("", text)
    text = LINE_COMMENT.sub("", text)
    return text


def test_no_native_modals_in_web_ui():
    offenders = []
    for root in SCAN_ROOTS:
        for path in sorted(list(root.rglob("*.html")) + list(root.rglob("*.js"))):
            text = path.read_text(encoding="utf-8")
            stripped = _strip_comments(text)
            for lineno, line in enumerate(stripped.splitlines(), 1):
                if NATIVE_MODAL.search(line):
                    rel = path.relative_to(REPO_ROOT)
                    offenders.append(f"{rel}:{lineno}: {line.strip()}")

    assert not offenders, (
        "Native confirm()/prompt()/alert() found in web UI. Use the custom "
        "modal helpers in cms/static/app.js (showConfirm/showPrompt/showToast) "
        "instead. See .github/copilot-instructions.md for the convention.\n\n"
        + "\n".join(offenders)
    )
