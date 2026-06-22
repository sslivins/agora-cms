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
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.asset import Asset
from shared.models.slideshow_slide import SlideshowSlide
from cms.schemas.asset import SLIDE_TRANSITIONS
from cms.services.slideshow_resolver import (
    _slide_window_open,
    expand_tag_members,
)


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
    drop_closed_at: datetime | None = None,
) -> list[tuple[SlideshowSlide, Asset | None]]:
    """Return the slideshow's slides in ``position`` order with their sources.

    The deck is the hybrid tag-timeline: an ordered list of
    :class:`~shared.models.slideshow_slide.SlideshowSlide` rows, each either
    a static ``asset`` slide or a dynamic ``tag`` block.  A ``tag`` block is
    expanded **in place** into its current members (via
    :func:`~cms.services.slideshow_resolver.expand_tag_members`, the same
    ordering the device resolver uses) so the caller never sees a tag row.
    Every expanded member reuses the *same* tag-block row, inheriting its
    playback columns (duration / transition / fit / effect) as deck-defaults
    — matching the device-resolve path.

    Each returned tuple is ``(slide, source_asset)``; ``source_asset`` is
    ``None`` only when a static ``asset`` slide's source row is missing (or,
    with ``exclude_deleted=True``, soft-deleted).  Callers decide whether a
    missing source is an error.  Tag-block members are always non-deleted
    (the expansion filters them), so they never yield a ``None`` source.
    Returns an empty list when the slideshow has no slides (or expands to
    none, e.g. only empty tag blocks).

    Per-slide visibility windows (manifest schema 1.5): when
    ``drop_closed_at`` is supplied (a tz-aware local-now), any slide whose
    visibility window is *closed* at that instant is dropped — so the
    caller sees only the slides that are live right now.  This is the
    device-faithful preview behaviour (the Pi drops closed slides too).
    The default ``None`` means "show every slide" so the device-publish
    path (which delegates window handling to the slideshow resolver) and
    the structural pickability check in the editor are unaffected.
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

    # Walk the hybrid deck in position order, expanding each ``tag`` block
    # into its ordered members.  A tag member inherits the block row's
    # playback columns, so we pair every member with the same row object.
    pairs: list[tuple[SlideshowSlide, uuid.UUID | None]] = []
    for s in slide_rows:
        if s.kind == "tag":
            if s.tag_id is None:  # defensive — CHECK constraint forbids
                continue
            for member_id in await expand_tag_members(s.tag_id, db):
                pairs.append((s, member_id))
        else:
            pairs.append((s, s.source_asset_id))

    if not pairs:
        return []

    src_ids = [mid for _s, mid in pairs if mid is not None]
    by_id: dict[uuid.UUID, Asset] = {}
    if src_ids:
        src_q = select(Asset).where(Asset.id.in_(src_ids))
        if exclude_deleted:
            src_q = src_q.where(Asset.deleted_at.is_(None))
        src_rows = (await db.execute(src_q)).scalars().all()
        by_id = {a.id: a for a in src_rows}

    result = [
        (s, by_id.get(mid) if mid is not None else None) for s, mid in pairs
    ]
    if drop_closed_at is not None:
        result = [
            pair for pair in result if _slide_window_open(pair[0], drop_closed_at)
        ]
    return result
