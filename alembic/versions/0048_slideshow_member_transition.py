"""slideshow_slides: per-member (tag-block) transition

Adds ``member_transition`` / ``member_transition_ms`` to
``slideshow_slides``.  These control the transition BETWEEN expanded
``tag``-block members (members 1..N of the timeline), distinct from the
existing ``transition`` which is the transition INTO the block (member
0).

Both columns are nullable with no server default.  NULL means "inherit
``transition``" — exactly the pre-feature behaviour where every member
shared the block's transition — so existing rows render byte-identically
and no backfill is required.  The CHECK constraints permit NULL and
otherwise constrain to the same transition vocabulary / duration bounds
as ``transition`` (mirrors ``cms.schemas.asset.SLIDE_TRANSITIONS`` and
the JS shell's ``SS_TRANSITIONS``).

Revision ID: 0048
Revises: 0047
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0048"
down_revision = "0047"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "slideshow_slides",
        sa.Column("member_transition", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "slideshow_slides",
        sa.Column("member_transition_ms", sa.Integer(), nullable=True),
    )
    op.create_check_constraint(
        "ck_slideshow_slide_member_transition_known",
        "slideshow_slides",
        "member_transition IS NULL OR member_transition IN "
        "('cut','fade','fade_black','dissolve','push','wipe','zoom')",
    )
    op.create_check_constraint(
        "ck_slideshow_slide_member_transition_ms_range",
        "slideshow_slides",
        "member_transition_ms IS NULL OR "
        "(member_transition_ms >= 0 AND member_transition_ms <= 5000)",
    )


def downgrade() -> None:
    raise NotImplementedError("downgrade of 0048 is not supported")
