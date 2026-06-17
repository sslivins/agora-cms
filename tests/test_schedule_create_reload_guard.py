"""Regression test for the spurious "Reload Site?" prompt after creating
a schedule.

Background
----------
Creating a schedule POSTs ``/api/schedules`` (which persists the row) and
then swaps the new row into the table in place via
``_replaceScheduleRow()`` BEFORE ``form.reset()`` clears the create
form's dirty state. ``_replaceScheduleRow`` has several
``location.reload()`` fallbacks that fire when the ``/row`` HTML-fragment
fetch transiently fails. Because the reload happens while the create form
is still dirty, the page's ``beforeunload`` guard (added so users don't
lose unsaved edits) pops the native "Reload Site? Changes you made may
not be saved." prompt — even though the schedule was already saved.

The fix exposes ``window.cwSuppressUnloadGuard()`` from schedules.html,
honored by the ``beforeunload`` handler, and calls it immediately before
every post-save programmatic reload (in both app.js and the edit-modal
path in schedules.html).

This test pins that wiring so the guard can't silently regress.
"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_APP_JS = _ROOT / "cms" / "static" / "app.js"
_SCHEDULES_HTML = _ROOT / "cms" / "templates" / "schedules.html"


def test_schedules_html_defines_suppress_guard():
    src = _SCHEDULES_HTML.read_text(encoding="utf-8")
    # The flag and the global setter must be defined.
    assert "let _suppressUnloadGuard = false;" in src
    assert "window.cwSuppressUnloadGuard = () =>" in src
    # The beforeunload handler must early-return when suppressed, and that
    # early-return must come before the dirty-check that triggers the prompt.
    assert "if (_suppressUnloadGuard) return;" in src
    guard_idx = src.index("if (_suppressUnloadGuard) return;")
    dirty_idx = src.index("_isCreateFormDirty() || _isEditModalDirty()")
    assert guard_idx < dirty_idx


def test_app_js_suppresses_guard_before_post_save_reloads():
    src = _APP_JS.read_text(encoding="utf-8")
    # Every location.reload() inside the schedule create/replace path must
    # be preceded by the suppress call (optional-chained because app.js is
    # shared across pages where the global may not exist).
    create_section = src[src.index("async function _replaceScheduleRow") :
                         src.index("// ── User & Role Management ──")]
    # Each reload in this section is a post-save programmatic reload.
    for chunk in create_section.split("location.reload();")[:-1]:
        assert "window.cwSuppressUnloadGuard?.();" in chunk.rsplit(";", 2)[-1] or \
            "cwSuppressUnloadGuard" in chunk[-120:], (
            "a post-save location.reload() in the schedule path is not "
            "preceded by window.cwSuppressUnloadGuard?.()"
        )
