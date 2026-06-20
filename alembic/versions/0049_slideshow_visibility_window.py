"""slideshow_slides: per-slide visibility window

Adds five nullable columns that restrict WHEN an individual slide is
eligible to be shown, plus three CHECK constraints mirroring the
Pydantic validators in ``cms.schemas.asset.SlideIn``:

* ``valid_from`` / ``valid_to`` (``date``) — local-calendar date range,
  INCLUSIVE both ends.  A single-day window (from == to) is allowed.
* ``active_days`` (``smallint[]``) — weekdays 0=Mon..6=Sun the slide may
  show on.  NULL or empty = every day.
* ``active_start`` / ``active_end`` (``time``) — local time-of-day
  window.  ``active_start`` is inclusive, ``active_end`` is exclusive;
  ``active_start > active_end`` wraps past midnight (e.g. 22:00..02:00).

All five columns are nullable with no server default — NULL means
"unrestricted on this axis", so an existing row (all five NULL) is
always visible and renders byte-identically.  No backfill required.

The window is evaluated server-side in the slideshow resolver against
the requesting device's effective local time; a slide whose window is
closed is dropped from the resolved deck.

Revision ID: 0049
Revises: 0048
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0049"
down_revision = "0048"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "slideshow_slides",
        sa.Column("valid_from", sa.Date(), nullable=True),
    )
    op.add_column(
        "slideshow_slides",
        sa.Column("valid_to", sa.Date(), nullable=True),
    )
    op.add_column(
        "slideshow_slides",
        sa.Column(
            "active_days",
            postgresql.ARRAY(sa.SmallInteger()),
            nullable=True,
        ),
    )
    op.add_column(
        "slideshow_slides",
        sa.Column("active_start", sa.Time(), nullable=True),
    )
    op.add_column(
        "slideshow_slides",
        sa.Column("active_end", sa.Time(), nullable=True),
    )
    # Date range coherent: single-day (from == to) allowed.
    op.create_check_constraint(
        "ck_slideshow_slide_valid_range",
        "slideshow_slides",
        "valid_to IS NULL OR valid_from IS NULL OR valid_to >= valid_from",
    )
    # Time window non-degenerate: equal start/end rejected; wrap allowed.
    op.create_check_constraint(
        "ck_slideshow_slide_time_window",
        "slideshow_slides",
        "active_start IS NULL OR active_end IS NULL OR active_start <> active_end",
    )
    # Weekday set stays within 0..6 (Mon..Sun).
    op.create_check_constraint(
        "ck_slideshow_slide_active_days",
        "slideshow_slides",
        "active_days IS NULL OR active_days <@ '{0,1,2,3,4,5,6}'::smallint[]",
    )


def downgrade() -> None:
    raise NotImplementedError("downgrade of 0049 is not supported")
