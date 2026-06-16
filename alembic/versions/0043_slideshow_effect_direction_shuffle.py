"""slideshow: per-slide Ken Burns direction + deck-level shuffle

Slideshow feature roadmap (agora#261), milestone 1.  Two additive
slideshow features that share one manifest schema bump (1.3 -> 1.4):

* ``slideshow_slides.effect_direction VARCHAR(16) NOT NULL DEFAULT
  'in'`` — the Ken Burns pan/zoom direction for a slide.  Only
  meaningful when ``effect == 'ken_burns'``.  ``in`` (default)
  reproduces the original zoom-in animation; the other presets are
  ``out`` / ``left`` / ``right`` / ``up`` / ``down``.  Enforced by
  ``cms.schemas.asset.KEN_BURNS_DIRECTIONS`` and a DB-level CHECK
  constraint, mirrored by the JS shell's allow-list in
  ``agora/player/shell/player.js``.
* ``assets.shuffle BOOLEAN NOT NULL DEFAULT false`` — deck-level
  flag for SLIDESHOW assets.  When true the device plays the slide
  order in a deterministic per-cycle shuffle.  Slideshow-only meaning
  (like ``duration_seconds``); default false for all other asset
  types.

Both DEFAULTs preserve pre-1.4 behaviour byte-for-byte (every row
picks up ``in`` / ``false``).  Neither column is folded into the
structural ``Asset.checksum`` content hash; the *resolved* per-device
manifest checksum folds ``effect_direction`` and ``shuffle`` (mirroring
how fit/effect are handled), so the hash rolls over only on actual
edits.

Revision ID: 0043
Revises: 0042
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "slideshow_slides",
        sa.Column(
            "effect_direction",
            sa.String(length=16),
            nullable=False,
            server_default="in",
        ),
    )
    op.create_check_constraint(
        "ck_slideshow_slide_effect_direction_known",
        "slideshow_slides",
        "effect_direction IN ('in','out','left','right','up','down')",
    )
    op.add_column(
        "assets",
        sa.Column(
            "shuffle",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    raise NotImplementedError("downgrade of 0043 is not supported")
