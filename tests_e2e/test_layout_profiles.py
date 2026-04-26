"""Layout/overflow regression tests for /profiles — issue #444 (PR5).

Mirrors :mod:`tests_e2e.test_layout_assets` for the Profiles page.
``/profiles`` renders one ``<tr data-profile-id="...">`` per profile
(both built-in and user-created) with a kebab menu in the last cell.
The same "kebab-off-the-side-of-the-table" class of bug applies; this
file extends the layout-regression coverage to it.

We deliberately scope to the **Profiles** card here. The Transcoding
Queue card on /profiles is JS-populated and admin-scoped, and built-in
profile rows render a different kebab menu shape (Reset instead of
Delete) — so the seeded row is a non-builtin profile and we always
target it by ``data-profile-id`` rather than ``.first``.

Profile names are constrained by ``cms/schemas/profile.py`` to a 64-char
regex (``^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$``). The fixture below seeds
the maximum allowed length to maximize layout signal even though the
name cannot be a free-form human-readable phrase like other layout
tests.

See sslivins/agora-cms#444 for design rationale.
"""

import uuid

import pytest
from playwright.sync_api import Page, expect

from tests_e2e._layout import (
    assert_closed_kebabs_in_cells,
    assert_no_horizontal_overflow,
    assert_open_kebab_in_viewport,
)


# ── Long-but-realistic strings drive the worst-case row width ──
#
# /profiles columns (admin view, with profiles:write):
#   Name | Codec/Profile | Resolution | FPS | Bitrate/CRF |
#   PixFmt | Color | Devices | Variants | Actions
# Wide content lives in Name (capped at 64 chars by the schema regex)
# and Color (e.g. "bt2020-pq (HDR10)"), with the rest fixed-width.
#
# Build a 64-char name template — the most width-stressing realistic
# value the schema will accept. Underscores are used over hyphens so
# the cell has fewer natural break opportunities (cells are already
# white-space: nowrap, but defence in depth).
_PREFIX = "layout_444_profiles_pi5_compute_module_h264_uhd_30fps_"
assert len(_PREFIX) == 54


@pytest.fixture
def _profile_seed(api):
    """Seed (or re-seed) one non-builtin profile with a width-stressing
    name. Robust to leftover state across runs.

    Returns ``{"id": <uuid>, "name": <str>}``.
    """
    suffix = uuid.uuid4().hex[:10]  # 10 hex chars
    name = f"{_PREFIX}{suffix}"
    assert len(name) == 64, f"seed name must be exactly 64 chars: {len(name)}"

    create_resp = api.post(
        "/api/profiles",
        json={"name": name, "video_codec": "h264"},
    )
    assert create_resp.status_code == 201, (
        f"create profile: {create_resp.status_code} {create_resp.text}"
    )
    return {"id": create_resp.json()["id"], "name": name}


# ── Page-load helper ──

def _goto_profiles(page: Page, profile_id: str) -> None:
    page.goto("/profiles")
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_selector(
        f'tr[data-profile-id="{profile_id}"]',
        timeout=5000,
    )


# Three desktop viewports — same matrix as the other layout tests.
_VIEWPORTS = [
    pytest.param(1024, 768, id="1024x768"),
    pytest.param(1366, 768, id="1366x768"),
    pytest.param(1440, 900, id="1440x900"),
]


@pytest.mark.e2e
class TestProfilesLayout:
    """Geometry assertions for the /profiles Profiles card."""

    @pytest.mark.parametrize("vw,vh", _VIEWPORTS)
    def test_no_overflow_profiles_table(
        self, page: Page, _profile_seed, vw, vh,
    ):
        """Profiles table must not push the page past the viewport and
        every closed kebab must stay inside its actions cell.
        """
        page.set_viewport_size({"width": vw, "height": vh})
        _goto_profiles(page, _profile_seed["id"])

        # Anchor by the card containing the seeded row by *id* — the
        # name is unique per fixture run but text-based filters are
        # fragile when prior runs leave similar names behind.
        profiles_card = page.locator(".card").filter(
            has=page.locator(
                f'tr[data-profile-id="{_profile_seed["id"]}"]'
            ),
        ).first
        expect(profiles_card).to_be_visible()

        assert_no_horizontal_overflow(
            page, label=f"@{vw}x{vh} profiles",
        )
        assert_closed_kebabs_in_cells(
            page,
            profiles_card.locator("table").first,
            label=f"@{vw}x{vh} profiles",
        )

    @pytest.mark.parametrize("vw,vh", _VIEWPORTS)
    def test_open_kebab_stays_in_viewport_profiles(
        self, page: Page, _profile_seed, vw, vh,
    ):
        """Open the kebab on the seeded profile's row — menu must stay
        in viewport and anchor near the trigger.
        """
        page.set_viewport_size({"width": vw, "height": vh})
        _goto_profiles(page, _profile_seed["id"])

        # Target the seeded row by id — built-in profile rows render a
        # different kebab menu shape (Reset instead of Delete), so we
        # never use ``.first`` across the whole table.
        target_row = page.locator(
            f'tr[data-profile-id="{_profile_seed["id"]}"]'
        ).first
        kebab = target_row.locator(".btn-kebab").first
        expect(kebab).to_be_visible()

        assert_open_kebab_in_viewport(
            page, kebab, label=f"@{vw}x{vh} profiles",
        )
