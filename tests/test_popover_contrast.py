"""Regression tests for popover visual contrast.

The kebab menu and group action popovers both sit *on top of* cards that
are already painted with the dark-blue `--primary` family. Early versions
styled the popovers with `border: 1px solid var(--primary)` (blue on blue)
and/or `background: var(--surface-alt)` (a very slightly lighter navy),
which made the popover edges nearly invisible against the page.

The fix is the same pattern used by `.card-highlight`: paint the popover
with the darker `--surface` and draw a neutral light border using
`rgba(255, 255, 255, 0.25)`. These tests pin both declarations so a stray
refactor of the dark-blue tokens can't silently re-introduce the
low-contrast styling.
"""

from pathlib import Path

import pytest

STYLE_CSS = Path(__file__).parent.parent / "cms" / "static" / "style.css"


@pytest.fixture(scope="module")
def css() -> str:
    return STYLE_CSS.read_text(encoding="utf-8")


def _rule_body(css: str, selector: str) -> str:
    """Return the body (text between `{` and matching `}`) for the first
    occurrence of ``selector`` in ``css``. Raises if the selector is
    missing so the test fails loudly when a refactor renames the rule."""
    anchor = f"{selector} {{"
    start = css.find(anchor)
    assert start != -1, f"CSS rule {selector!r} not found in style.css"
    end = css.find("}", start)
    assert end != -1, f"CSS rule {selector!r} has no closing brace"
    return css[start:end]


def test_kebab_menu_uses_light_contrast_border(css: str) -> None:
    body = _rule_body(css, ".kebab-menu[popover]")
    assert "rgba(255, 255, 255, 0.25)" in body, (
        "Kebab popover border must use the standard light contrast color "
        "(rgba(255, 255, 255, 0.25)) — the dark-blue --border token blends "
        "into the page. Rule body was:\n" + body
    )
    assert "rgba(255, 255, 255, 0.08)" in body and "var(--surface)" in body, (
        "Kebab popover background must layer a translucent white overlay "
        "(rgba(255, 255, 255, 0.08)) over var(--surface) so the menu reads "
        "as lifted above surrounding cards."
    )
    assert "rgba(255, 255, 255, 0.08)" in body, (
        "Kebab popover overlay must be ~8% white over --surface so the menu "
        "reads as lifted above surrounding cards without overpowering them."
    )


def test_group_popup_uses_light_contrast_border(css: str) -> None:
    body = _rule_body(css, ".group-popup[popover]")
    assert "rgba(255, 255, 255, 0.25)" in body, (
        "Group action popover border must use the standard light contrast "
        "color (rgba(255, 255, 255, 0.25)). Rule body was:\n" + body
    )
    assert "rgba(255, 255, 255, 0.08)" in body and "var(--surface)" in body, (
        "Group action popover background must layer a translucent white "
        "overlay over var(--surface). --surface-alt alone is too close to "
        "the --primary family and loses contrast against neighbouring cards."
    )
    assert "rgba(255, 255, 255, 0.08)" in body, (
        "Group action popover overlay must be at least 20% white so it "
        "reads as distinctly lifted above the page."
    )


def test_border_token_still_blue_family(css: str) -> None:
    """Sanity check: the --border token is intentionally aliased to the
    dark-blue --primary in the current palette. If that ever changes, the
    popover rules above can be simplified to reference var(--border)
    again — delete this test at that time and update the popover rules."""
    # Find the :root block.
    root_idx = css.find(":root")
    assert root_idx != -1
    block = css[root_idx : css.find("}", root_idx)]
    assert "--border: #0f3460" in block or "--border: var(--primary)" in block, (
        "This test documents why the popovers don't just use var(--border): "
        "the --border token is aliased to the dark-blue --primary and would "
        "disappear against the page. If you've recoloured --border to a "
        "neutral, update/remove this test."
    )
