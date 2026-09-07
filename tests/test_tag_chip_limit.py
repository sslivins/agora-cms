"""The chip collapse limit is duplicated by necessity — keep it honest.

`tag_chips()` in `_macros.html` renders the collapsed state server-side (so
there's no expand-then-collapse flash on load) while `TagPicker` in `app.js`
re-applies it after any client-side re-render. If the two numbers drift, a row
silently shows a different number of chips depending on how it got there.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _macro_limit() -> int:
    src = (ROOT / "cms" / "templates" / "_macros.html").read_text(encoding="utf-8")
    macro = src.split("{% macro tag_chips(", 1)[1]
    m = re.search(r"\{%\s*set\s+_limit\s*=\s*(\d+)\s*%\}", macro)
    assert m, "tag_chips() no longer defines a _limit"
    return int(m.group(1))


def _js_limit() -> int:
    src = (ROOT / "cms" / "static" / "app.js").read_text(encoding="utf-8")
    m = re.search(r"const\s+CHIP_LIMIT\s*=\s*(\d+)\s*;", src)
    assert m, "TagPicker no longer defines CHIP_LIMIT"
    return int(m.group(1))


def test_chip_limit_matches_between_template_and_js():
    assert _macro_limit() == _js_limit()


def test_overflow_chips_stay_in_the_dom():
    """Hidden chips must be hidden with CSS, never dropped.

    appliedIds()/tagNames() read the chips out of the DOM, and assignGroup()
    uses tagNames() to warn about tags lost on a group move. Removing overflow
    chips instead of hiding them would silently break both.
    """
    css = (ROOT / "cms" / "static" / "style.css").read_text(encoding="utf-8")
    assert ".tag-chips-collapsed .tag-chip[data-tag-overflow]" in css
    assert "display: none" in css.split(
        ".tag-chips-collapsed .tag-chip[data-tag-overflow]", 1)[1][:60]
