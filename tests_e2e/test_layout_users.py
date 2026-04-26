"""Layout/overflow regression tests for /users — issue #444 (PR3).

Mirrors :mod:`tests_e2e.test_layout_devices` for the Users page.
``/users`` renders a server-rendered Users table (one row per user)
with a kebab menu in the last cell of each row.  The same
"kebab-off-the-side-of-the-table" class of bug applies; this file
extends the layout-regression coverage to it.

We deliberately scope to the **Users** card here.  The Roles card uses
a kebab menu but it lives inside a flex ``role-actions`` div, not a
table cell, so the table-cell-clipping failure mode does not apply.
The admin API-keys table is JS-populated and would need extra seeding
(API key creation) — deferred.

Note: the current user's row on /users renders a different menu shape
(no disable/delete for self, see ``users.html``).  The fixture below
seeds a dedicated non-self user and the tests target *that* row by
unique email, never ``.first``, so the assertions stay stable
regardless of admin sort order or other test users.

See sslivins/agora-cms#444 for design rationale.
"""

import pytest
from playwright.sync_api import Page, expect

from tests_e2e._layout import (
    assert_closed_kebabs_in_cells,
    assert_no_horizontal_overflow,
    assert_open_kebab_in_viewport,
)


# ── Long-but-realistic strings drive the worst-case row width ──
#
# /users columns (admin view, with users:write):
#   Email | Display Name | Role | Status | Last Login | Actions
# Wide content lives in Email and Display Name.
_LAYOUT_USER_EMAIL = "layout-444-users-display-name-stress@e2e.fake.local"
_LAYOUT_USER_DISPLAY = (
    "Layout #444 — Building 92 — North Wing — Hallway Display Operator"
)


@pytest.fixture
def _users_seed(api):
    """Seed (or re-seed) one Operator user with a width-stressing
    email + display name.  Robust to leftover state across runs.
    """
    # Seed minimal SMTP so the user-creation endpoint is unblocked.
    api.post("/api/settings/smtp", json={
        "host": "smtp.fake.local",
        "port": 587,
        "from_email": "noreply@fake.local",
    })

    roles = api.get("/api/roles").json()
    operator_id = next(r["id"] for r in roles if r["name"] == "Operator")

    resp = api.post("/api/users", json={
        "email": _LAYOUT_USER_EMAIL,
        "display_name": _LAYOUT_USER_DISPLAY,
        "password": "LayoutTest444!",
        "role_id": operator_id,
        "group_ids": [],
    })
    if resp.status_code == 201:
        uid = resp.json()["id"]
    elif resp.status_code == 409:
        all_users = api.get("/api/users").json()
        uid = next(u["id"] for u in all_users if u["email"] == _LAYOUT_USER_EMAIL)
    else:  # pragma: no cover — diagnostic only
        raise AssertionError(f"create user: {resp.status_code} {resp.text}")

    # Force the long display name regardless of prior state, and
    # disable the must-change-password gate so /users renders
    # normally for this row.
    api.patch(f"/api/users/{uid}", json={
        "display_name": _LAYOUT_USER_DISPLAY,
        "must_change_password": False,
    })

    return {"id": uid, "email": _LAYOUT_USER_EMAIL}


# ── Page-load helper ──

def _goto_users(page: Page, target_email: str) -> None:
    page.goto("/users")
    page.wait_for_load_state("domcontentloaded")
    # The seeded user's row must be present before we measure.
    page.wait_for_function(
        """(email) => {
            const rows = document.querySelectorAll('tbody tr');
            for (const r of rows) {
                if (r.textContent && r.textContent.includes(email)) return true;
            }
            return false;
        }""",
        arg=target_email,
        timeout=5000,
    )


# Three desktop viewports — same matrix as the other layout tests.
_VIEWPORTS = [
    pytest.param(1024, 768, id="1024x768"),
    pytest.param(1366, 768, id="1366x768"),
    pytest.param(1440, 900, id="1440x900"),
]


@pytest.mark.e2e
class TestUsersLayout:
    """Geometry assertions for the /users page."""

    @pytest.mark.parametrize("vw,vh", _VIEWPORTS)
    def test_no_overflow_users_table(
        self, page: Page, _users_seed, vw, vh,
    ):
        """Users table must not push the page past the viewport and
        every closed kebab must stay inside its actions cell.
        """
        page.set_viewport_size({"width": vw, "height": vh})
        _goto_users(page, _users_seed["email"])

        # Anchor by the card containing the seeded user — "Create
        # User" card also has-text "Users", so we filter precisely.
        users_card = page.locator(".card").filter(
            has=page.get_by_text(_users_seed["email"]),
        ).first
        expect(users_card).to_be_visible()

        assert_no_horizontal_overflow(
            page, label=f"@{vw}x{vh} users",
        )
        assert_closed_kebabs_in_cells(
            page,
            users_card.locator("table").first,
            label=f"@{vw}x{vh} users",
        )

    @pytest.mark.parametrize("vw,vh", _VIEWPORTS)
    def test_open_kebab_stays_in_viewport_users(
        self, page: Page, _users_seed, vw, vh,
    ):
        """Open the kebab on the seeded user's row — menu must stay in
        viewport and anchor near the trigger.
        """
        page.set_viewport_size({"width": vw, "height": vh})
        _goto_users(page, _users_seed["email"])

        users_card = page.locator(".card").filter(
            has=page.get_by_text(_users_seed["email"]),
        ).first
        # Target the seeded row by its unique email — never .first,
        # because the current admin user's row has a different shape.
        target_row = users_card.locator(
            "tbody tr", has_text=_users_seed["email"],
        ).first
        kebab = target_row.locator(".btn-kebab").first
        expect(kebab).to_be_visible()

        assert_open_kebab_in_viewport(
            page, kebab, label=f"@{vw}x{vh} users",
        )
