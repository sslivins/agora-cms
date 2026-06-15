"""slideshow_slides: per-slide fit + effect columns

Phase 5 slideshow feature (per-slide display effects, manifest schema
1.3).  Adds two new columns to ``slideshow_slides``:

* ``fit VARCHAR(16) NOT NULL DEFAULT 'cover'`` — how the slide's media
  is fitted into the frame.  ``cover`` (fill, may crop) or ``contain``
  (letterbox, no crop).  Enforced by the Pydantic input schema
  (``cms.schemas.asset.SLIDE_FITS``) and a DB-level CHECK constraint.
* ``effect VARCHAR(16) NOT NULL DEFAULT 'none'`` — an optional per-slide
  display effect.  ``none`` (static) or ``ken_burns`` (slow zoom across
  the slide's display time).  Enforced by
  ``cms.schemas.asset.SLIDE_EFFECTS`` and a DB-level CHECK constraint.

Mirrors 0028 (per-slide transitions).  The DEFAULTs ensure existing
rows pick up the pre-1.3 behaviour (``cover`` / ``none`` — byte-
identical rendering).  The manifest content hash folds fit/effect, so
the hash rolls over once at deploy time (every row picks up the same
defaults) and then only on actual edits.

Revision ID: 0041
Revises: 0040
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "slideshow_slides",
        sa.Column(
            "fit",
            sa.String(length=16),
            nullable=False,
            server_default="cover",
        ),
    )
    op.add_column(
        "slideshow_slides",
        sa.Column(
            "effect",
            sa.String(length=16),
            nullable=False,
            server_default="none",
        ),
    )
    op.create_check_constraint(
        "ck_slideshow_slide_fit_known",
        "slideshow_slides",
        "fit IN ('cover','contain')",
    )
    op.create_check_constraint(
        "ck_slideshow_slide_effect_known",
        "slideshow_slides",
        "effect IN ('none','ken_burns')",
    )


def downgrade() -> None:
    raise NotImplementedError("downgrade of 0041 is not supported")
