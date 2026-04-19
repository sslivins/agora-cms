"""Regression tests for issue #248: error toasts must be hard to miss.

The toast component used to:
  - Treat the 2nd arg strictly as a boolean, which meant
    `showToast("ok", "success")` rendered as an error (string 'success'
    is truthy) — a latent bug in users.html that this PR also fixes.
  - Auto-dismiss all toasts (including errors) after 3s with no dismiss
    button, so failed actions were easy to miss.

These source-level checks lock in the new contract:
  - Error toasts use a longer lifetime (≥ 6s) and the shake-in animation.
  - `showToast` accepts 'error' / 'success' / 'warning' / 'info' strings.
  - Every toast gets a dismiss button.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "cms" / "static" / "app.js"
STYLE_CSS = ROOT / "cms" / "static" / "style.css"


def test_show_toast_accepts_string_variants():
    src = APP_JS.read_text(encoding="utf-8")
    # New signature normalizes variant from strings.
    assert 'v === "error"' in src
    assert 'v === "success"' in src
    assert 'v === "warning"' in src
    # Shouldn't silently treat 'success' (truthy string) as error anymore.
    assert "isError = false" not in src, (
        "showToast still uses the old truthy-boolean signature; "
        "showToast('x', 'success') will render as an error (regression of #248)"
    )


def test_show_toast_emits_dismiss_button():
    src = APP_JS.read_text(encoding="utf-8")
    assert 'class = "toast-close"' in src or 'className = "toast-close"' in src
    assert 'setAttribute("aria-label", "Dismiss")' in src


def test_error_toasts_linger_longer_than_success():
    src = APP_JS.read_text(encoding="utf-8")
    # Error toasts should be well over the old 3000ms default.
    assert "isError ? 7000" in src or "isError ? 6000" in src or "isError ? 8000" in src, (
        "error toast lifetime was not extended; they'll still disappear "
        "before users notice them (regression of #248)"
    )


def test_error_toast_has_shake_animation():
    css = STYLE_CSS.read_text(encoding="utf-8")
    assert "@keyframes shakeIn" in css
    assert ".toast.toast-error" in css
    assert "shakeIn" in css.split(".toast.toast-error", 1)[1][:600], (
        ".toast-error no longer uses the shakeIn animation (regression of #248)"
    )


def test_error_toast_role_alert():
    """Error toasts should be assertive ARIA live regions for screen readers."""
    src = APP_JS.read_text(encoding="utf-8")
    assert 'role", isError ? "alert" : "status"' in src
    assert 'aria-live", isError ? "assertive"' in src
