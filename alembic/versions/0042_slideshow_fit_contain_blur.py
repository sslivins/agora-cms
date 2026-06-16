"""slideshow_slides: allow 'contain_blur' fit

Slideshow feature roadmap (agora#261), milestone 1.  Widens the
``ck_slideshow_slide_fit_known`` CHECK constraint to accept a new
per-slide fit value ``contain_blur`` in addition to ``cover`` /
``contain``.

``contain_blur`` renders the image contained (whole frame visible)
over a blurred, zoomed ``cover`` backdrop of the same image so the
letterbox bars are filled rather than black.  It is additive: existing
rows keep ``cover`` and a pre-blur device parser that doesn't recognise
the value degrades gracefully to plain ``contain``.

Mirrors the allow-list in ``cms.schemas.asset.SLIDE_FITS`` and the JS
shell's ``KNOWN_FITS`` in ``agora/player/shell/player.js``.

Revision ID: 0042
Revises: 0041
"""

from __future__ import annotations

from alembic import op

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_slideshow_slide_fit_known",
        "slideshow_slides",
        type_="check",
    )
    op.create_check_constraint(
        "ck_slideshow_slide_fit_known",
        "slideshow_slides",
        "fit IN ('cover','contain','contain_blur')",
    )


def downgrade() -> None:
    raise NotImplementedError("downgrade of 0042 is not supported")
