"""SlideshowTagRule model — binds a SLIDESHOW asset to a tag (agora-cms#806).

A *tag-mode* slideshow has no explicit ``slideshow_slides`` rows.  Its
content is resolved live at sync-build time from the set of assets
currently carrying ``tag_id``.  The presence of a ``SlideshowTagRule``
row for a slideshow asset is therefore the mode switch: the resolver
checks for one and, if found, builds the deck from the tag membership
instead of from ``SlideshowSlide`` rows.

Ordering & seamless insertion (Option B — append at tail)
---------------------------------------------------------
v1 orders members by **tag-membership creation time**
(``asset_tags.created_at ASC``).  This is not merely a default — it is
load-bearing for the "tag an asset and it appears in the running
slideshow without a restart" guarantee:

* The firmware player has no mutable playhead.  The on-screen slide is a
  pure function ``(now - started_at) % cycle_duration`` over the slide
  list.  Any change to the list or cycle length shifts that modulo and
  would make the deck jump — unless the anchor is preserved AND the new
  slide lands at a position the playhead has not yet reached this cycle.
* Ordering by membership time means a newly-tagged asset always sorts to
  the **tail** of the deck.  Combined with a persisted, never-re-floored
  anchor (:attr:`anchor_at`), every existing slide keeps its offset in
  the cycle, so the currently-displayed slide never moves; the new slide
  first appears at the end of the in-flight cycle.

Other orderings (alphabetical, upload time, …) could slot a new asset
into the *middle* of the deck, shifting slides behind the playhead and
breaking the no-restart guarantee.  They are intentionally deferred; v1
exposes ``tagged_at`` only.

Tags are CMS-only metadata, so this model lives in ``cms.models`` (not
``shared.models``) alongside :class:`cms.models.tag.Tag` — the worker
never resolves slideshows.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from cms.database import Base

# Default per-slide display time for a tag-mode deck (8 s) — a sensible
# image dwell.  Manual decks set this per slide; a tag deck has no per
# slide UI, so the dwell is a single deck-level default on the rule.
DEFAULT_TAG_SLIDE_DURATION_MS = 8000

# v1 supports a single ordering — see the module docstring for why
# ``tagged_at`` is required (not merely default) for seamless insertion.
SLIDESHOW_TAG_ORDER_BY_VALUES = ("tagged_at",)


class SlideshowTagRule(Base):
    """Binds a SLIDESHOW asset to a tag; presence => tag-mode deck."""

    __tablename__ = "slideshow_tag_rules"
    __table_args__ = (
        CheckConstraint(
            "order_by IN ('tagged_at')",
            name="ck_slideshow_tag_rule_order_by_known",
        ),
        CheckConstraint(
            "default_duration_ms > 0",
            name="ck_slideshow_tag_rule_duration_pos",
        ),
    )

    # One rule per slideshow asset — the asset id IS the primary key, which
    # enforces the 1:1 (a slideshow is either manual or tag-mode, never
    # both) and gives the resolver an index-free PK lookup.
    slideshow_asset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("assets.id", ondelete="CASCADE"),
        primary_key=True,
    )
    tag_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tags.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # How members are ordered within the deck.  See module docstring —
    # ``tagged_at`` is the only v1 value and is required for the seamless
    # tail-append guarantee.
    order_by: Mapped[str] = mapped_column(
        String(32), nullable=False, default="tagged_at", server_default="tagged_at"
    )

    # Deck-level default per-slide playback settings.  A tag deck has no
    # per-slide authoring UI, so these apply uniformly to every resolved
    # member.  Defaults mirror SlideshowSlide's column defaults so a tag
    # deck behaves like a hand-built one with default slides.
    default_duration_ms: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=DEFAULT_TAG_SLIDE_DURATION_MS,
        server_default=str(DEFAULT_TAG_SLIDE_DURATION_MS),
    )
    default_transition: Mapped[str] = mapped_column(
        String(16), nullable=False, default="cut", server_default="cut"
    )
    default_transition_ms: Mapped[int] = mapped_column(
        Integer, nullable=False, default=600, server_default="600"
    )
    default_fit: Mapped[str] = mapped_column(
        String(16), nullable=False, default="cover", server_default="cover"
    )
    default_effect: Mapped[str] = mapped_column(
        String(16), nullable=False, default="none", server_default="none"
    )
    default_effect_direction: Mapped[str] = mapped_column(
        String(16), nullable=False, default="in", server_default="in"
    )

    # Persisted wall-clock cycle anchor (Option B stability).  Set once when
    # the rule is created and thereafter NEVER re-floored, so appending a
    # newly-tagged slide at the tail leaves every existing slide's offset
    # in the cycle unchanged — the on-screen slide does not move.  The
    # resolver emits this verbatim as the manifest ``started_at``.  Nullable
    # so a defensively-created rule (or an old row) falls back to the
    # per-build floor in the resolver.
    anchor_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=func.current_timestamp(),
    )
