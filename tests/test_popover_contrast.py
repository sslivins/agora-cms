"""Regression tests for popover visual contrast.

The kebab menu and group action popovers both sit *on top of* cards that
are already painted with the dark-blue `--primary` family. Early versions
styled the popovers with `border: 1px solid var(--primary)` (blue on blue)
and/or `background: var(--surface-alt)` (a very slightly lighter navy),
which made the popover edges nearly invisible against the page.

The fix is the same pattern used by `.card-highlight`: paint the popover
with the darker `--surface` plus a translucent contrast overlay, and draw
a neutral contrast border. Since the introduction of the light theme these
are expressed with the theme-aware overlay tokens rather than raw white
literals:

    border:     1px solid var(--overlay-strong)
    background: linear-gradient(var(--overlay-hover), var(--overlay-hover)),
                var(--surface)

In the dark theme ``--overlay-strong``/``--overlay-hover`` resolve to
``rgba(255,255,255,0.25)``/``rgba(255,255,255,0.08)`` (the historical
values); in the light theme they flip to black overlays so the border
stays visible against a light page. These tests pin the token usage so a
stray refactor can't silently re-introduce the low-contrast styling or
hardcode a single-theme literal.
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
    assert "var(--overlay-strong)" in body, (
        "Kebab popover border must use the theme-aware contrast token "
        "var(--overlay-strong) — the dark-blue --border token blends into "
        "the page, and a raw white literal would vanish on a light page. "
        "Rule body was:\n" + body
    )
    assert "var(--border)" not in body and "var(--primary)" not in body, (
        "Kebab popover border must not use the dark-blue --border/--primary "
        "tokens (blue-on-blue, nearly invisible). Rule body was:\n" + body
    )
    assert "var(--overlay-hover)" in body and "var(--surface)" in body, (
        "Kebab popover background must layer the translucent overlay token "
        "var(--overlay-hover) over var(--surface) so the menu reads as "
        "lifted above surrounding cards in both themes."
    )


def test_group_popup_uses_light_contrast_border(css: str) -> None:
    body = _rule_body(css, ".group-popup[popover]")
    assert "var(--overlay-strong)" in body, (
        "Group action popover border must use the theme-aware contrast token "
        "var(--overlay-strong). Rule body was:\n" + body
    )
    assert "var(--border)" not in body and "var(--primary)" not in body, (
        "Group action popover border must not use the dark-blue "
        "--border/--primary tokens. Rule body was:\n" + body
    )
    assert "var(--overlay-hover)" in body and "var(--surface)" in body, (
        "Group action popover background must layer var(--overlay-hover) "
        "over var(--surface). --surface-alt alone is too close to the "
        "--primary family and loses contrast against neighbouring cards."
    )


def test_overlay_tokens_match_legacy_dark_values(css: str) -> None:
    """The overlay tokens used by the popovers must resolve to the historical
    white-contrast literals in the dark (`:root`) theme, so the dark-mode
    appearance is unchanged by the move from raw literals to tokens."""
    root_idx = css.find(":root")
    assert root_idx != -1
    block = css[root_idx : css.find("}", root_idx)]
    assert "--overlay-strong: rgba(255,255,255,0.25)" in block, (
        "Dark --overlay-strong must stay rgba(255,255,255,0.25) so the "
        "popover border matches its historical appearance."
    )
    assert "--overlay-hover: rgba(255,255,255,0.08)" in block, (
        "Dark --overlay-hover must stay rgba(255,255,255,0.08) so the "
        "popover background overlay matches its historical appearance."
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
