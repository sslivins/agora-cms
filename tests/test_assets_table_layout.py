"""Smoke tests for the assets library table layout.

Guards the structure of ``cms/templates/assets.html`` so regressions to
column placement fail loudly. Current columns:

    [▶] | Name | Type | Scope | Size | Status | Duration | Actions

The "Shared" badge (displayed for assets shared with the current user
via a group, where the user isn't the owner) lives in the **Name**
column next to the filename — it's an ownership indicator, not an
action, so it doesn't belong in Actions. This matches the conventions
of Google Drive / Dropbox / OneDrive.
"""

from pathlib import Path

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "cms" / "templates"
ASSETS_HTML = (TEMPLATES_DIR / "assets.html").read_text(encoding="utf-8")


def test_shared_badge_is_in_name_column_not_actions():
    """The Shared badge must render inside the Name cell, not Actions."""
    # There must be exactly one Shared badge markup in the file (we used
    # to have it in the Actions column — that placement is gone).
    shared_markers = ASSETS_HTML.count('class="badge badge-processing" style="font-size:0.75rem">Shared<')
    assert shared_markers == 1, (
        f"Expected exactly one 'Shared' badge in assets.html (in the Name "
        f"column). Found {shared_markers}. If you moved it, update this test."
    )

    # The actions cell must not contain the Shared badge anymore. Find the
    # `<td class="actions"` block and verify "Shared" doesn't appear in it
    # until the next `</td>`.
    idx = ASSETS_HTML.find('<td class="actions"')
    assert idx >= 0, "actions column <td> not found"
    end = ASSETS_HTML.find("</td>", idx)
    actions_block = ASSETS_HTML[idx:end]
    assert ">Shared<" not in actions_block, (
        "The Shared badge must not live in the Actions column — it's an "
        "ownership indicator, not an action. Move it to the Name column."
    )


def test_shared_badge_set_is_row_scoped():
    """``is_owner`` / ``shared_badge`` must be defined on the row (outside
    the Name cell) so they're still in scope for the Actions column,
    which uses ``is_owner`` to gate delete/recapture buttons.
    """
    # The {% set is_owner %} must appear before the <tr ... asset-row> that
    # starts each row so the variable is in scope for the whole row.
    is_owner_idx = ASSETS_HTML.find("{% set is_owner = ")
    tr_idx = ASSETS_HTML.find('<tr class="asset-row"')
    assert is_owner_idx >= 0 and tr_idx >= 0, (
        "Expected `{% set is_owner %}` and `<tr class=\"asset-row\">` in "
        "assets.html."
    )
    assert is_owner_idx < tr_idx, (
        "`is_owner` must be defined before the `<tr class=\"asset-row\">` "
        "opens, so it's in scope for every cell in the row (including the "
        "Actions column, which uses it to gate owner-only controls)."
    )


def test_name_cell_renders_shared_badge():
    """The Name cell (`asset-filename-cell`) must include the Shared badge."""
    # Isolate the Name cell's <td> block and verify the badge lives inside.
    idx = ASSETS_HTML.find('<td class="wrap asset-filename-cell">')
    assert idx >= 0, "Name column <td> not found in assets.html"
    end = ASSETS_HTML.find("</td>", idx)
    name_block = ASSETS_HTML[idx:end]
    assert "shared_badge" in name_block and ">Shared<" in name_block, (
        "The Shared badge must be rendered inside the Name column "
        "(`asset-filename-cell`), next to the filename."
    )
