"""Smoke tests for asset-type icons.

Each AssetType has a distinct emoji icon (🎬 video, 🖼️ image, 🌐 webpage,
📡 stream, 📼 saved stream) so users can tell asset types apart at a glance
in the library, schedule list, and device dropdowns.

This file guards the feature end-to-end:

1. The ``asset_icon`` Jinja filter returns the right emoji for every
   AssetType (this has regressed before — see PR that added this file).
2. The filter is registered on the Jinja environment so templates can
   actually use it.
3. Every asset type in ``AssetType`` has an icon — adding a new type
   without an icon will fail here instead of silently shipping blanks.
4. User-facing templates (assets list, schedules list, schedules JS
   dropdown) actually reference the icons. If someone deletes the
   ``asset_icon`` usage or reverts the JS mapping, this catches it.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from cms.models.asset import AssetType
from cms.ui import _ASSET_ICONS, asset_icon, templates


EXPECTED_ICONS = {
    "video": "🎬",
    "image": "📷",
    "webpage": "🌐",
    "stream": "📡",
    "saved_stream": "📼",
}


# ---------------------------------------------------------------------------
# Filter behaviour
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("type_value,expected_icon", list(EXPECTED_ICONS.items()))
def test_asset_icon_filter_returns_expected_emoji_for_string(type_value, expected_icon):
    assert asset_icon(type_value) == expected_icon


@pytest.mark.parametrize("asset_type", list(AssetType))
def test_asset_icon_filter_accepts_enum_members(asset_type):
    # Every enum member must resolve to a non-empty icon.
    assert asset_icon(asset_type) != ""
    assert asset_icon(asset_type) == EXPECTED_ICONS[asset_type.value]


@pytest.mark.parametrize("asset_type", list(AssetType))
def test_asset_icon_filter_accepts_asset_like_objects(asset_type):
    asset = SimpleNamespace(asset_type=asset_type, duration_seconds=None)
    assert asset_icon(asset) == EXPECTED_ICONS[asset_type.value]


def test_asset_icon_filter_handles_none():
    assert asset_icon(None) == ""


def test_asset_icon_filter_handles_unknown_type():
    # Unknown types must not raise — they just get no icon.
    assert asset_icon("something_new") == ""


def test_every_asset_type_has_an_icon():
    """If a new AssetType ships without an icon mapping, fail loudly."""
    missing = [t.value for t in AssetType if t.value not in _ASSET_ICONS]
    assert not missing, (
        f"AssetType(s) without an icon mapping in cms.ui._ASSET_ICONS: {missing}. "
        "Add an entry or confirm the icon is intentionally blank."
    )


def test_asset_icon_mapping_matches_expected_set():
    """Lock the icon set so accidental deletions/renames fail here."""
    assert _ASSET_ICONS == EXPECTED_ICONS


# ---------------------------------------------------------------------------
# Jinja environment wiring
# ---------------------------------------------------------------------------


def test_asset_icon_filter_is_registered():
    assert "asset_icon" in templates.env.filters
    assert templates.env.filters["asset_icon"] is asset_icon


def test_asset_label_suffix_filter_is_registered():
    # The dropdown-suffix filter (which calls asset_icon under the hood)
    # must stay registered too — templates that rely on it would otherwise
    # render nothing.
    assert "asset_label_suffix" in templates.env.filters


def test_asset_icon_renders_via_jinja_filter_syntax():
    """End-to-end: a template using ``{{ asset | asset_icon }}`` works."""
    tpl = templates.env.from_string("{{ a | asset_icon }}")
    asset = SimpleNamespace(asset_type=AssetType.WEBPAGE, duration_seconds=None)
    assert tpl.render(a=asset) == "🌐"


# ---------------------------------------------------------------------------
# Template usage (regression guard — the feature existed before and was removed)
# ---------------------------------------------------------------------------

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "cms" / "templates"


def _read(name: str) -> str:
    return (TEMPLATES_DIR / name).read_text(encoding="utf-8")


def test_assets_table_badge_uses_asset_icon_filter():
    """The Type column in the assets library must render the icon.

    The per-asset row was extracted to the _macros.asset_row macro as part
    of the #87 no-reload work, so check both assets.html and _macros.html.
    """
    body = _read("assets.html") + _read("_macros.html")
    assert "a | asset_icon" in body, (
        "asset_row macro (in _macros.html) must use the asset_icon filter on each "
        "row's type badge. If you moved it, update this test accordingly."
    )


def test_schedules_list_uses_asset_icon_filter():
    """Both active + expired schedule lists must render the asset icon."""
    # The active row was extracted to _macros.active_schedule_row as part
    # of the #87 schedules-page no-reload refactor. Check both templates.
    body = _read("schedules.html") + _read("_macros.html")
    # We expect the filter to be used at least twice (active + expired tables).
    assert body.count("s.asset | asset_icon") >= 2, (
        "schedules.html / _macros.html must render the asset icon for each "
        "schedule row (active and expired tables)."
    )


def test_schedules_js_asset_icons_map_is_defined():
    """The inline JS ASSET_ICONS map must cover all five asset types so
    option labels in the asset dropdowns show icons — and the mapping must
    stay in sync with the Python side.
    """
    body = _read("schedules.html")
    assert "const ASSET_ICONS" in body, (
        "schedules.html must define a shared ASSET_ICONS JS map so dropdown "
        "option labels can render icons for every asset type."
    )
    # Each icon must appear in the JS block so we don't silently drop one.
    for type_value, emoji in EXPECTED_ICONS.items():
        assert emoji in body, (
            f"schedules.html is missing the icon {emoji!r} for asset type "
            f"{type_value!r}. Keep the JS ASSET_ICONS map in sync with "
            "cms.ui._ASSET_ICONS."
        )


# ---------------------------------------------------------------------------
# Dropdown icon placement (must be a PREFIX, not suffix)
# ---------------------------------------------------------------------------


def _render(source: str, **ctx) -> str:
    return templates.env.from_string(source).render(**ctx)


def test_asset_label_suffix_returns_only_duration():
    """After the prefix fix, asset_label_suffix is duration-only. No emoji.

    The icon is rendered separately as a prefix via ``asset_icon`` so it
    appears *in front of* the asset name in dropdown options.
    """
    for t in AssetType:
        suffix = templates.env.filters["asset_label_suffix"](
            SimpleNamespace(asset_type=t, duration_seconds=None)
        )
        for emoji in EXPECTED_ICONS.values():
            assert emoji not in suffix, (
                f"asset_label_suffix({t.value}) unexpectedly contains icon "
                f"{emoji!r} — icons must be rendered as a prefix via "
                "asset_icon, not appended in the suffix."
            )


def test_dropdown_option_renders_icon_before_name():
    """End-to-end: `{{ a | asset_icon }} {{ name }}{{ a | asset_label_suffix }}`
    must produce `🎬 name (5:32)` — icon first, duration last."""
    asset = SimpleNamespace(asset_type=AssetType.VIDEO, duration_seconds=332)
    tpl = "{{ a | asset_icon }} {{ name }}{{ a | asset_label_suffix }}"
    out = _render(tpl, a=asset, name="birthday.mp4")
    assert out == "🎬 birthday.mp4 (5:32)"
    # And specifically: icon must come before the name.
    assert out.index("🎬") < out.index("birthday.mp4")


def test_dropdown_option_webpage_renders_icon_only_as_prefix():
    asset = SimpleNamespace(asset_type=AssetType.WEBPAGE, duration_seconds=None)
    tpl = "{{ a | asset_icon }} {{ name }}{{ a | asset_label_suffix }}"
    out = _render(tpl, a=asset, name="status-page")
    assert out == "🌐 status-page"


@pytest.mark.parametrize(
    "template_file,must_contain_before_name",
    [
        # devices.html has 2 option blocks (per-device + per-group defaults);
        # both must prefix the icon.
        ("devices.html", 2),
        # schedules.html server-rendered asset dropdown.
        ("schedules.html", 1),
    ],
)
def test_option_templates_prefix_icon_before_name(
    template_file, must_contain_before_name
):
    """Guard the placement: ``{{ a | asset_icon }} {{ a.display_name ...`` must
    appear for every asset dropdown. If someone reverts the prefix back to
    a suffix, this catches it.
    """
    body = _read(template_file)
    # The group-default dropdown was extracted to macros.group_panel as part
    # of the #87 no-reload work, so also scan _macros.html for devices.html.
    if template_file == "devices.html":
        body += _read("_macros.html")
    pattern = "{{ a | asset_icon }} {{ a.display_name"
    count = body.count(pattern)
    assert count >= must_contain_before_name, (
        f"{template_file}: expected at least {must_contain_before_name} "
        f"dropdown option(s) to prefix the asset name with the icon "
        f"(pattern: '{{{{ a | asset_icon }}}} {{{{ a.display_name ...'), "
        f"found {count}."
    )


def test_schedules_js_prefixes_icon_before_name():
    """Both inline-JS dropdown builders must prepend the icon to ``a.name``
    (the label string), not append it.
    """
    body = _read("schedules.html")
    # The icon must be composed in front of a.name in both builders.
    assert body.count("icon + ' ' + a.name") >= 2, (
        "schedules.html inline JS must prefix ASSET_ICONS[a.type] before "
        "a.name (expected two dropdown builders to use "
        "`icon + ' ' + a.name`)."
    )
    # Regression guard: no builder should be appending the icon after the name.
    assert "a.name + ' ' + icon" not in body, (
        "schedules.html inline JS is appending the icon after the name — "
        "icons must be a prefix."
    )
    assert "label += ' ' + icon" not in body, (
        "schedules.html inline JS still has a 'label += icon' suffix style; "
        "icons must be prepended to the label instead."
    )
