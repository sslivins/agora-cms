"""Regression test for issue #196.

The variant table in the asset detail panel used to map API response rows
to DOM rows by array index. When the /api/assets/status endpoint returned
variants in a different order on successive polls (e.g. as statuses
flipped from processing → ready), progress from one variant could render
on another variant's row, making it impossible to tell which variant was
actually transcoding.

The fix: give each variant row a stable `data-variant-id` attribute
sourced from the variant UUID, and match by that ID in the polling JS
rather than by array index.

These are lightweight source-level checks so the contract can't silently
regress. Full UI integration tests live elsewhere.
"""
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parents[1] / "cms" / "templates" / "assets.html"


def test_variant_row_has_stable_data_variant_id():
    """The Jinja-rendered variant row must include data-variant-id."""
    src = TEMPLATE.read_text(encoding="utf-8")
    # The loop body is the only place we render variant rows inside the
    # live-variant-table tbody.
    assert "{% for v in a.visible_variants %}" in src
    # Must render data-variant-id on the <tr>
    assert 'data-variant-id="{{ v.id }}"' in src, (
        "variant row <tr> is missing data-variant-id; polling JS will fall "
        "back to index-based matching and swap progress between rows "
        "(regression of #196)"
    )


def test_update_variant_table_matches_by_id_not_index():
    """Polling JS must look up rows by data-variant-id, not rows[i]."""
    src = TEMPLATE.read_text(encoding="utf-8")
    # The ID-based selector must be present.
    assert (
        'tr[data-variant-id="\' + v.id + \'"]' in src
    ), "updateVariantTable no longer matches by variant ID (regression of #196)"
    # The old index-based pattern must be gone so we don't regress silently.
    assert "rows[i].querySelectorAll" not in src, (
        "updateVariantTable still has index-based row lookup; this caused "
        "#196 where progress jumped between variant rows during polling."
    )
