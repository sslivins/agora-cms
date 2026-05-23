"""slideshow_slides: per-slide transition + transition_ms columns

Phase 1a of agora#226 (wall-clock slideshow + manifest schema versioning).

Adds two new columns to ``slideshow_slides``:

* ``transition VARCHAR(16) NOT NULL DEFAULT 'cut'`` — which transition the
  player should run BEFORE the slide appears (i.e. the transition is
  attached to the slide on the right of a gap).  Allowed values:
  ``cut``, ``fade``, ``dissolve``, ``wipe`` — enforced both by the
  Pydantic input schema (``cms.schemas.asset.SLIDE_TRANSITIONS``) and a
  DB-level CHECK constraint as the belt-and-braces guard against
  out-of-band INSERTs.
* ``transition_ms INTEGER NOT NULL DEFAULT 600`` — duration of the
  transition in milliseconds.  ``0`` is valid (and required for
  ``cut``).  Upper bound 5000 ms matches
  ``cms.schemas.asset.MAX_SLIDE_TRANSITION_MS``.

The DEFAULTs ensure existing rows pick up the pre-Phase-1a behaviour
(``cut`` / ``600 ms`` — which the mpv player renders as an instant
swap anyway since it ignores anything other than ``cut``).  The
manifest content hash WILL change for slideshows once the columns are
backfilled (a transition edit is a user-visible content change and we
want devices to refetch), but since every existing row picks up the
same defaults, the hash effectively rolls over once at deploy time and
then only on actual edits.

Revision ID: 0028
Revises: 0027
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "slideshow_slides",
        sa.Column(
            "transition",
            sa.String(length=16),
            nullable=False,
            server_default="cut",
        ),
    )
    op.add_column(
        "slideshow_slides",
        sa.Column(
            "transition_ms",
            sa.Integer(),
            nullable=False,
            server_default="600",
        ),
    )
    op.create_check_constraint(
        "ck_slideshow_slide_transition_known",
        "slideshow_slides",
        "transition IN ('cut','fade','dissolve','wipe')",
    )
    op.create_check_constraint(
        "ck_slideshow_slide_transition_ms_range",
        "slideshow_slides",
        "transition_ms >= 0 AND transition_ms <= 5000",
    )


def downgrade() -> None:
    raise NotImplementedError("downgrade of 0028 is not supported")
