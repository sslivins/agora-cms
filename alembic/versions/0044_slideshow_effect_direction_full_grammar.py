"""slideshow: widen Ken Burns effect_direction CHECK to full grammar

The Ken Burns authoring UI (slideshow builder) emits the full direction
grammar — a ZOOM (``in``/``out``) plus an optional PAN over 8 compass
directions including diagonals, e.g. ``out_up_right`` (which is now the
authoring default for a new ken_burns slide).  The Pydantic schema
(``cms.schemas.asset.KEN_BURNS_DIRECTIONS``) and the JS player shell have
allowed this full set since agora#261, but the DB CHECK constraint
created in migration 0043 only permitted the original six tokens
``('in','out','left','right','up','down')``.

As a result, saving a slide with any diagonal or zoom+pan direction
passed Pydantic validation but violated ``ck_slideshow_slide_effect_
direction_known`` on INSERT/UPDATE, surfacing as an HTTP 500
(IntegrityError) — i.e. *every* Ken Burns save failed once the UI shipped
the new default.  This migration drops and recreates the constraint with
the full 26-token grammar so the DB guard matches the schema.

Additive / non-destructive: the new allowed set is a strict superset of
the old one, so no existing row can violate it and no data migration is
needed.  ``VARCHAR(16)`` already accommodates the longest token
(``out_down_right`` = 14 chars).

Revision ID: 0044
Revises: 0043
"""

from __future__ import annotations

from alembic import op

revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None

_CONSTRAINT = "ck_slideshow_slide_effect_direction_known"

# Canonical Ken Burns direction grammar.  Frozen copy of
# ``cms.schemas.asset.KEN_BURNS_DIRECTIONS`` (and the matching tuple in
# ``shared.models.slideshow_slide``) as of this revision — migrations must
# be self-contained and must not import live app code, so the list is
# materialised here rather than imported.
_PANS = (
    "left",
    "right",
    "up",
    "down",
    "up_left",
    "up_right",
    "down_left",
    "down_right",
)
_DIRECTIONS = (
    "in",
    "out",
    *(f"in_{_p}" for _p in _PANS),
    *(f"out_{_p}" for _p in _PANS),
    *_PANS,
)
_NEW_LIST = ",".join(f"'{_d}'" for _d in _DIRECTIONS)
_OLD_LIST = "'in','out','left','right','up','down'"


def upgrade() -> None:
    op.drop_constraint(_CONSTRAINT, "slideshow_slides", type_="check")
    op.create_check_constraint(
        _CONSTRAINT,
        "slideshow_slides",
        f"effect_direction IN ({_NEW_LIST})",
    )


def downgrade() -> None:
    # Reverting would re-narrow the allowed set; any row written with a
    # diagonal/zoom+pan direction while 0044 was applied would then violate
    # the constraint.  Mirror 0043's stance and refuse to downgrade.
    raise NotImplementedError("downgrade of 0044 is not supported")
