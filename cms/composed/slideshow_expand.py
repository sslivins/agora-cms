"""Shared helpers for embedding a SLIDESHOW asset in a media widget.

A :class:`~cms.composed.widgets.media.MediaWidget` may point its single
``asset_id`` at a SLIDESHOW asset.  A slideshow has no file of its own —
it is an ordered sequence of IMAGE/VIDEO *source* assets.  Both the
device-publish path (:mod:`cms.composed.publish`) and the preview /
thumbnail path (:mod:`cms.composed.render`) need to:

* load the slideshow's slides in ``position`` order, and
* route each slide's source asset into the bundle exactly the way a
  standalone media asset of that type would be routed.

The per-source routing differs between the two callers (publish ships
videos as device-local sibling URLs; render inlines them as base64
``data:`` URIs), so this module deliberately does **not** do the
routing — it only loads the ordered ``(slide, source_asset)`` pairs and
maps the slide's transition into the composed-cell repertoire.  Each
caller then loops the sources through its own existing image/video
routing and builds the
:class:`~cms.composed.registry.SlideshowSlidePlan` list.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.asset import Asset
from shared.models.slideshow_slide import SlideshowSlide
from cms.schemas.asset import SLIDE_TRANSITIONS


def composed_cell_transition(transition: str) -> str:
    """Map a slideshow transition into the composed-cell repertoire.

    The composed media cell now renders the **full** slideshow
    transition set as self-contained CSS keyframes / JS inside the
    Chromium bundle: ``cut`` (instant), ``fade`` (cross-fade),
    ``fade_black`` (through-black), ``dissolve`` (Ken Burns),
    ``push`` (slide), ``wipe`` (clip reveal) and ``zoom``.  These are
    self-contained approximations of the device's native firmware
    renderer — close enough that an embedded slideshow reads the same
    as the standalone one.

    Any value outside :data:`~cms.schemas.asset.SLIDE_TRANSITIONS`
    (should be impossible — the slide schema validates it) falls back
    to ``"fade"`` so the cell still cycles rather than emitting an
    unknown class.
    """
    return transition if transition in SLIDE_TRANSITIONS else "fade"


async def load_slideshow_members(
    db: AsyncSession,
    slideshow_asset_id: uuid.UUID,
    *,
    exclude_deleted: bool,
) -> list[tuple[SlideshowSlide, Asset | None]]:
    """Return the slideshow's slides in ``position`` order with their sources.

    Each tuple is ``(slide, source_asset)``; ``source_asset`` is ``None``
    when the slide's source row is missing (or, with
    ``exclude_deleted=True``, soft-deleted).  Callers decide whether a
    missing source is an error.  Returns an empty list when the slideshow
    has no slides.
    """
    slide_rows = (
        await db.execute(
            select(SlideshowSlide)
            .where(SlideshowSlide.slideshow_asset_id == slideshow_asset_id)
            .order_by(SlideshowSlide.position.asc())
        )
    ).scalars().all()
    if not slide_rows:
        return []

    src_ids = [s.source_asset_id for s in slide_rows]
    src_q = select(Asset).where(Asset.id.in_(src_ids))
    if exclude_deleted:
        src_q = src_q.where(Asset.deleted_at.is_(None))
    src_rows = (await db.execute(src_q)).scalars().all()
    by_id = {a.id: a for a in src_rows}

    return [(s, by_id.get(s.source_asset_id)) for s in slide_rows]
