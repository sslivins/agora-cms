"""Alembic migration 0047 — retire ``slideshow_tag_rules`` (Phase 1).

Phase 0 (migration 0046) added the hybrid tag-timeline columns to
``slideshow_slides`` but deliberately left the legacy 1:1
``slideshow_tag_rules`` table (0045) in place and un-migrated.  Phase 1
finishes the job: every legacy tag-rule row becomes a single ``kind='tag'``
``slideshow_slides`` entry on its slideshow, the rule's persisted cycle
anchor moves to ``assets.slideshow_anchor_at`` (already added in 0046),
and the now-empty side table is dropped.

Data migration, per legacy ``slideshow_tag_rules`` row:

* Insert one ``slideshow_slides`` row with ``kind='tag'``,
  ``source_asset_id = NULL``, ``tag_id`` / ``tag_order_by`` carried from the
  rule, and the rule's ``default_*`` playback columns mapped onto the slide's
  per-slide columns.  A tag-mode slideshow had **no** ``slideshow_slides``
  rows (the rule itself was the deck), so the new block lands at the next
  free ``position`` (``COALESCE(MAX(position)+1, 0)``) — defensive even
  though that is ``0`` for every legacy tag deck.
* Copy the rule's ``anchor_at`` onto the parent asset's
  ``slideshow_anchor_at`` so a running tag deck keeps its cycle phase.

Then drop the index and the table.

This path only ever runs against Postgres (the unit-test harness builds the
schema via ``Base.metadata.create_all``, bypassing migrations), so
``gen_random_uuid()`` (Postgres 13+ core) and ``INSERT … SELECT`` are fine.

Revision ID: 0047
Revises: 0046
"""

from __future__ import annotations

from alembic import op

revision = "0047"
down_revision = "0046"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1) Carry each rule's persisted cycle anchor onto its slideshow asset.
    #    (assets.slideshow_anchor_at was added in 0046.)
    op.execute(
        """
        UPDATE assets AS a
        SET slideshow_anchor_at = r.anchor_at
        FROM slideshow_tag_rules AS r
        WHERE r.slideshow_asset_id = a.id
          AND r.anchor_at IS NOT NULL
        """
    )

    # 2) Convert each legacy tag-rule into a single kind='tag' slide row.
    op.execute(
        """
        INSERT INTO slideshow_slides (
            id,
            slideshow_asset_id,
            kind,
            source_asset_id,
            tag_id,
            tag_order_by,
            position,
            duration_ms,
            play_to_end,
            transition,
            transition_ms,
            fit,
            effect,
            effect_direction,
            created_at
        )
        SELECT
            gen_random_uuid(),
            r.slideshow_asset_id,
            'tag',
            NULL,
            r.tag_id,
            r.order_by,
            COALESCE(
                (
                    SELECT MAX(s.position) + 1
                    FROM slideshow_slides AS s
                    WHERE s.slideshow_asset_id = r.slideshow_asset_id
                ),
                0
            ),
            r.default_duration_ms,
            FALSE,
            r.default_transition,
            r.default_transition_ms,
            r.default_fit,
            r.default_effect,
            r.default_effect_direction,
            r.created_at
        FROM slideshow_tag_rules AS r
        """
    )

    # 3) Drop the now-empty legacy side table.
    op.drop_index("ix_slideshow_tag_rules_tag_id", table_name="slideshow_tag_rules")
    op.drop_table("slideshow_tag_rules")


def downgrade() -> None:
    raise NotImplementedError("downgrade of 0047 is not supported")
