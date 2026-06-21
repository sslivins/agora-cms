"""slideshow_slides: per-slide video clip range

Adds two nullable columns that restrict a VIDEO slide to a sub-range of
its source asset, plus two CHECK constraints mirroring the Pydantic
validators in ``cms.schemas.asset.SlideIn``:

* ``clip_start_ms`` (``integer``) — offset into the source the device
  seeks to before playing.  NULL = start at 0.
* ``clip_duration_ms`` (``integer``) — how long to play from that offset.
  NULL = play to the natural end of the source.

Both columns are nullable with no server default — both NULL means
"play the whole source / honour play_to_end", so an existing row renders
byte-identically.  No backfill required.

The clip is turned into the emitted per-slide ``duration_ms`` slot plus
the wire ``clip_start_ms`` seek in the slideshow resolver, validated
there against the probed ``Asset.duration_seconds`` real length (which
is not knowable at the DB layer, hence only shape CHECKs here).

Revision ID: 0050
Revises: 0049
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0050"
down_revision = "0049"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "slideshow_slides",
        sa.Column("clip_start_ms", sa.Integer(), nullable=True),
    )
    op.add_column(
        "slideshow_slides",
        sa.Column("clip_duration_ms", sa.Integer(), nullable=True),
    )
    # Offset non-negative.
    op.create_check_constraint(
        "ck_slideshow_slide_clip_start_nonneg",
        "slideshow_slides",
        "clip_start_ms IS NULL OR clip_start_ms >= 0",
    )
    # Clip length strictly positive.
    op.create_check_constraint(
        "ck_slideshow_slide_clip_duration_pos",
        "slideshow_slides",
        "clip_duration_ms IS NULL OR clip_duration_ms > 0",
    )


def downgrade() -> None:
    raise NotImplementedError("downgrade of 0050 is not supported")
