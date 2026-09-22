"""Asset-type UI parity: every AssetType must be fully wired into the library.

Companion to ``test_asset_icons.py``, which guards the emoji side. This file
guards the *other* three places a new asset type has to be registered before
it looks right in the asset library:

1. ``style.css`` needs a ``.badge-<type>`` colour rule. Without it the type
   badge renders with no background at all, unlike every other type.
2. The badge label must stay short enough for the Type column and the narrow
   grid cards.
3. ``_components/asset_filter_bar.html`` needs an ``<option>`` so the type can
   actually be filtered for.

All three were missed when ``voice_announcement`` shipped: the badge had no
colour, the label read "Voice Announcement", and there was no filter option
(fixed in the PR that added this file). These are static assertions, so they
run in the ordinary unit suite with no browser or DB.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from cms.models.asset import AssetType


_ROOT = Path(__file__).resolve().parent.parent
_CSS = _ROOT / "cms" / "static" / "style.css"
_TEMPLATES = _ROOT / "cms" / "templates"
_FILTER_BAR = _TEMPLATES / "_components" / "asset_filter_bar.html"

# Badge labels are uppercased by CSS (``text-transform: uppercase``) and share
# the Type column with the icon, so they need to stay compact. "Saved Stream"
# (12 chars) is the established ceiling.
_MAX_BADGE_LABEL_CHARS = 12


def _css() -> str:
    return _CSS.read_text(encoding="utf-8")


@pytest.mark.parametrize("asset_type", list(AssetType))
def test_every_asset_type_has_a_badge_colour(asset_type) -> None:
    """A missing ``.badge-<type>`` rule renders a badge with no background."""
    rule = f".badge-{asset_type.value}"
    assert re.search(rf"^\s*{re.escape(rule)}\s*\{{", _css(), re.MULTILINE), (
        f"style.css has no '{rule}' rule, so {asset_type.value} assets render "
        "an unstyled type badge. Add a background colour alongside the other "
        ".badge-<type> rules."
    )


def test_badge_colours_are_distinct() -> None:
    """Two asset types sharing a colour are indistinguishable in the library."""
    seen: dict[str, str] = {}
    css = _css()
    for asset_type in AssetType:
        m = re.search(
            rf"^\s*\.badge-{re.escape(asset_type.value)}\s*\{{([^}}]*)\}}",
            css,
            re.MULTILINE,
        )
        if not m:
            continue  # covered by the test above
        bg = re.search(r"background:\s*([^;]+);", m.group(1))
        if not bg:
            continue
        colour = bg.group(1).strip()
        assert colour not in seen, (
            f"{asset_type.value} reuses the badge colour {colour} already used "
            f"by {seen[colour]}; asset types must be visually distinct."
        )
        seen[colour] = asset_type.value


@pytest.mark.parametrize("asset_type", list(AssetType))
def test_every_asset_type_is_filterable(asset_type) -> None:
    """A type with no filter option cannot be searched for in the library."""
    body = _FILTER_BAR.read_text(encoding="utf-8")
    assert f'value="{asset_type.value}"' in body, (
        f"_components/asset_filter_bar.html has no <option> for "
        f"{asset_type.value}, so those assets cannot be filtered for."
    )


def test_badge_labels_stay_short() -> None:
    """Long labels wrap the Type column and overflow the grid cards."""
    macros = (_TEMPLATES / "_macros.html").read_text(encoding="utf-8")
    # The Type-column badge renders labels via an inline if/elif chain.
    badge_line = next(
        (l for l in macros.splitlines() if 'class="badge badge-{{ a.asset_type.value }}"' in l),
        None,
    )
    assert badge_line, "could not locate the asset_row type badge in _macros.html"

    for label in re.findall(r"%\}([A-Za-z][A-Za-z ]*?)\{%", badge_line):
        label = label.strip()
        if not label:
            continue
        assert len(label) <= _MAX_BADGE_LABEL_CHARS, (
            f"Type badge label {label!r} is {len(label)} chars; keep it to "
            f"{_MAX_BADGE_LABEL_CHARS} or fewer so it fits the Type column and "
            "the narrow grid cards."
        )


def test_voice_announcement_badge_label_is_short_form() -> None:
    """Regression fence for the specific label that shipped too long."""
    macros = (_TEMPLATES / "_macros.html").read_text(encoding="utf-8")
    assets = (_TEMPLATES / "assets.html").read_text(encoding="utf-8")
    assert "Voice Announcement" not in macros, (
        "The Type badge should read 'Voice', not 'Voice Announcement' — the "
        "long form wraps the Type column."
    )
    assert "voice_announcement: 'voice announcement'" not in assets, (
        "assets.html _typeLabel() should use the short 'voice' form for the "
        "grid cards, which are too narrow for the long label."
    )
