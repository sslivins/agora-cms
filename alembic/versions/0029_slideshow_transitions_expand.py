"""slideshow_slides: expand allowed ``transition`` set

Adds three new pro-grade transition IDs to the
``ck_slideshow_slide_transition_known`` CHECK constraint:

* ``fade_black`` — fade through black (two-stage opacity)
* ``push``      — incoming slide pushes outgoing off-screen (translateX)
* ``zoom``      — incoming scales 0.9 → 1.0 + opacity 0 → 1

The previously-allowed ``cut`` / ``fade`` / ``dissolve`` / ``wipe`` IDs
remain valid. ``dissolve`` is re-cast as a Ken-Burns variant in the
JS shell (crossfade + outgoing scales 1.00 → 1.05), but the wire ID
is unchanged so no data migration is needed.

Revision ID: 0029
Revises: 0028
"""

from __future__ import annotations

from alembic import op


revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


ALLOWED = ("cut", "fade", "fade_black", "dissolve", "push", "wipe", "zoom")
NEW_SQL = "transition IN (" + ",".join(f"'{v}'" for v in ALLOWED) + ")"


def upgrade() -> None:
    op.drop_constraint(
        "ck_slideshow_slide_transition_known",
        "slideshow_slides",
        type_="check",
    )
    op.create_check_constraint(
        "ck_slideshow_slide_transition_known",
        "slideshow_slides",
        NEW_SQL,
    )


def downgrade() -> None:
    raise NotImplementedError("downgrade of 0029 is not supported")
