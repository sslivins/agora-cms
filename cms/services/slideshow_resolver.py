"""Slideshow resolver + readiness diagnostics.

A *slideshow* asset is a synthetic Asset whose content is an ordered list
of existing IMAGE/VIDEO source assets resolved on the device.  At
``FETCH_ASSET`` time the CMS resolves each slide to the best READY
variant for the device's transcode profile (or the source itself for a
profile-less device), inlining a ``SlideDescriptor`` list in the outer
``FetchAssetMessage``.

This module exposes one shared planner so the resolver and the readiness
diagnostics endpoint can't drift:

* :func:`plan_slideshow` returns a :class:`SlideshowPlan` containing the
  resolved slide list and any blockers that prevent playback.
* :func:`build_fetch_for_slideshow` calls the planner and, if there are
  no blockers, builds a ready-to-send :class:`FetchAssetMessage` plus the
  per-device resolved manifest checksum used to dedup schedule pushes
  (see :func:`build_device_sync` integration).
* :func:`slideshow_readiness` returns a JSON-friendly readiness summary
  for the API and builder UI.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional, Sequence
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import logging

from cms.models.asset import Asset, AssetType, AssetVariant, VariantStatus
from cms.models.device import Device
from cms.models.tag import AssetTag
from cms.schemas.protocol import (
    FetchAssetMessage,
    SLIDESHOW_MANIFEST_SCHEMA_VERSION_LATEST,
    Sibling,
    SlideDescriptor,
)
from cms.services.asset_readiness import composed_unpublished_reason
from cms.services.storage import get_storage
from shared.models.slideshow_slide import SlideshowSlide

logger = logging.getLogger("agora.cms.slideshow")


# Blocker statuses returned by the planner — exposed via API so the UI
# can render an actionable message per slide.
BLOCKER_SOURCE_DELETED = "source_deleted"
BLOCKER_VARIANT_PROCESSING = "variant_processing"
BLOCKER_VARIANT_FAILED = "variant_failed"
BLOCKER_VARIANT_CANCELLED = "variant_cancelled"
# A composed-slide member that has never been published (no rendered
# bundle / checksum) — the device would 404 trying to download it.
BLOCKER_SOURCE_UNPUBLISHED = "source_unpublished"


@dataclass
class _SiblingPlan:
    """One resolved composed-slide sibling (referenced image/video).

    Mirrors the standalone-composed sibling contract
    (:func:`cms.services.device_inbound._build_composed_siblings`) but
    carries the storage *path* pieces rather than a baked download URL so
    the device-profile-specific URL can be built in
    :func:`build_fetch_for_slideshow` (which has ``storage``/``base_url``)
    while the planner stays device-light.
    """

    source_asset_id: uuid.UUID
    filename: str
    asset_type_value: str  # "image" / "video" / "saved_stream"
    checksum: str
    size_bytes: int
    download_path: str  # storage path for get_device_download_url
    api_url_path: str  # CMS-relative API URL for fallback


@dataclass
class _SlidePlan:
    """One resolved slide entry — internal to this module."""

    position: int
    source_asset_id: uuid.UUID
    source_filename: str
    source_asset_type: AssetType
    duration_ms: int
    play_to_end: bool
    # Per-slide transition (Phase 1a of agora#226).  Default ``cut`` / 600
    # ms matches the pre-Phase-1a behaviour for slides created before the
    # column existed.
    transition: str = "cut"
    transition_ms: int = 600
    # Per-slide display effects.  Default ``cover`` / ``none`` matches the
    # pre-effects behaviour for slides created before the columns existed.
    fit: str = "cover"
    effect: str = "none"
    # Ken Burns pan/zoom direction.  Default ``in`` matches the pre-1.4
    # zoom-in behaviour for slides created before the column existed.
    effect_direction: str = "in"
    # Populated for ready slides only:
    download_path: Optional[str] = None  # storage path for get_device_download_url
    api_url_path: Optional[str] = None  # CMS-relative API URL for fallback
    checksum: Optional[str] = None
    size_bytes: Optional[int] = None
    # Composed-slide members (Phase 5) carry their referenced media as
    # siblings the device pre-fetches before showing the bundle.  Empty
    # for image/video slides.
    siblings: list[_SiblingPlan] = field(default_factory=list)
    # Per-slide visibility window (manifest schema 1.5).  Populated only on
    # the device-evaluated (capability) path; None on the Phase-1 server-drop
    # path so the resolved checksum and emitted descriptor stay byte-identical
    # for devices without ``slideshow_visibility_v1``.  Carry the typed DB
    # values; serialization to wire strings happens at emit/fold time.
    valid_from: Optional[date] = None
    valid_to: Optional[date] = None
    active_days: Optional[list[int]] = None
    active_start: Optional[time] = None
    active_end: Optional[time] = None


@dataclass
class _SlideSpec:
    """A slide's *source-and-playback* description, before variant
    resolution.  Abstracts over both row kinds so
    :func:`plan_slideshow` can iterate one uniform list:

    * static ``asset`` rows map each ``SlideshowSlide`` row → one spec;
    * dynamic ``tag`` rows (agora#806) expand to one spec per currently
      tagged asset, filling playback fields from the tag block's columns.
    """

    position: int
    source_asset_id: uuid.UUID
    duration_ms: int
    play_to_end: bool
    transition: str
    transition_ms: int
    fit: str
    effect: str
    effect_direction: str
    # True when this spec was expanded from a dynamic ``tag`` block.  The
    # block owns a single dwell time for all members, but a VIDEO member
    # should still play its full natural length rather than being truncated
    # to that dwell — so the resolver flips ``play_to_end`` on for video
    # members once the member's asset type is known (agora#806 follow-up).
    tag_member: bool = False
    # Per-slide visibility window — carried only on the capability path
    # (``emit_windows=True`` in :func:`_load_slide_specs`); None otherwise.
    valid_from: Optional[date] = None
    valid_to: Optional[date] = None
    active_days: Optional[list[int]] = None
    active_start: Optional[time] = None
    active_end: Optional[time] = None

@dataclass
class SlideshowBlocker:
    slide_position: int
    source_asset_id: uuid.UUID
    source_filename: str
    status: str  # one of the BLOCKER_* constants


@dataclass
class SlideshowPlan:
    """Output of :func:`plan_slideshow` — resolved plan or blockers."""

    slides: list[_SlidePlan] = field(default_factory=list)
    blockers: list[SlideshowBlocker] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        # A 0-slide slideshow is never ready: a draft (e.g. just created by
        # the AI assistant) with no slides must not be pushed to a device,
        # which would render an empty manifest. Manual saves require >=1
        # slide client-side, so this only guards the empty-draft case.
        return bool(self.slides) and not self.blockers


# Sentinel returned by :func:`_plan_composed_siblings` when at least one
# referenced sibling has a non-terminal (in-flight) variant for the
# device profile.  Distinguished from an empty/None sibling set so the
# caller can raise a ``variant_processing`` blocker for the whole slide.
_SIBLINGS_INFLIGHT = "INFLIGHT"


async def _resolve_composed_sibling(
    ref: Asset, profile_id: Optional[uuid.UUID], db: AsyncSession
) -> Optional[_SiblingPlan]:
    """Resolve one referenced sibling asset to a :class:`_SiblingPlan`.

    Latest-READY-wins for the device profile (matching
    :func:`cms.services.device_inbound._resolve_variant_or_source`);
    falls back to the source asset when the device has no profile or no
    variant exists.  Returns ``None`` when a variant exists for this
    (asset, profile) pair but none is READY yet — the caller MUST treat
    that as "sibling in flight, skip the whole slideshow fetch".
    """
    is_file_asset = ref.asset_type in (
        AssetType.VIDEO,
        AssetType.IMAGE,
        AssetType.SAVED_STREAM,
    )
    type_value = ref.asset_type.value
    if is_file_asset and profile_id is not None:
        ready = (
            await db.execute(
                select(AssetVariant)
                .where(
                    AssetVariant.source_asset_id == ref.id,
                    AssetVariant.profile_id == profile_id,
                    AssetVariant.status == VariantStatus.READY,
                    AssetVariant.deleted_at.is_(None),
                )
                .order_by(
                    AssetVariant.created_at.desc(),
                    AssetVariant.id.desc(),
                )
                .limit(1)
            )
        ).scalars().first()
        if ready is not None:
            return _SiblingPlan(
                source_asset_id=ref.id,
                filename=ref.filename,
                asset_type_value=type_value,
                checksum=ready.checksum or "",
                size_bytes=ready.size_bytes or 0,
                download_path=f"variants/{ready.filename}",
                api_url_path=f"/api/assets/variants/{ready.id}/download",
            )
        inflight = (
            await db.execute(
                select(AssetVariant.id)
                .where(
                    AssetVariant.source_asset_id == ref.id,
                    AssetVariant.profile_id == profile_id,
                    AssetVariant.deleted_at.is_(None),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if inflight is not None:
            return None  # in-flight — caller raises a blocker

    return _SiblingPlan(
        source_asset_id=ref.id,
        filename=ref.filename,
        asset_type_value=type_value,
        checksum=ref.checksum or "",
        size_bytes=ref.size_bytes or 0,
        download_path=ref.filename,
        api_url_path=f"/api/assets/{ref.id}/download",
    )


async def _plan_composed_siblings(
    composed_asset: Asset, profile_id: Optional[uuid.UUID], db: AsyncSession
) -> "list[_SiblingPlan] | str | None":
    """Resolve the referenced media of a composed-slide member.

    Returns:
      * ``None`` when the bundle declares no source assets (all-text
        slide) — the slide ships with no siblings.
      * the string :data:`_SIBLINGS_INFLIGHT` when any sibling has a
        non-terminal variant for ``profile_id`` — caller raises a
        ``variant_processing`` blocker for the whole slide.
      * a list of :class:`_SiblingPlan` otherwise.  Missing (deleted)
        siblings are logged and skipped, matching the standalone-composed
        contract (the bundle plays with a broken media reference).
    """
    # Lazy import — avoids a module-import cycle with cms.composed / models.
    from cms.models.composed_slide import ComposedSlide

    cs_row = (
        await db.execute(
            select(ComposedSlide).where(
                ComposedSlide.asset_id == composed_asset.id
            )
        )
    ).scalars().first()

    raw_declared = list(cs_row.bundle_source_asset_ids or []) if cs_row else []
    if not raw_declared:
        return None

    # publish.py stores these as str(uuid); coerce back so the IN clause
    # and the by_id lookup hit the same Asset.id type.
    declared: list[uuid.UUID] = [
        aid if isinstance(aid, uuid.UUID) else uuid.UUID(str(aid))
        for aid in raw_declared
    ]

    rows = await db.execute(
        select(Asset).where(
            Asset.id.in_(declared),
            Asset.deleted_at.is_(None),
        )
    )
    by_id = {a.id: a for a in rows.scalars().all()}

    siblings: list[_SiblingPlan] = []
    for aid in declared:
        ref = by_id.get(aid)
        if ref is None:
            logger.warning(
                "Composed slideshow member %s references missing/deleted "
                "sibling asset %s; device will play bundle with broken "
                "media reference",
                composed_asset.id,
                aid,
            )
            continue
        resolved = await _resolve_composed_sibling(ref, profile_id, db)
        if resolved is None:
            return _SIBLINGS_INFLIGHT
        siblings.append(resolved)

    return siblings


# Leaf, directly-displayable asset types a tag-mode deck may include.
# SLIDESHOW is excluded to prevent nesting/recursion; WEBPAGE is excluded
# (not a cacheable single-file display asset on this path).
_TAG_MEMBER_ASSET_TYPES = (
    AssetType.IMAGE,
    AssetType.VIDEO,
    AssetType.SAVED_STREAM,
    AssetType.COMPOSED,
)


def _slide_window_open(row: SlideshowSlide, local_now: datetime) -> bool:
    """Return True iff ``row``'s per-slide visibility window is open at
    ``local_now`` (a tz-aware datetime in the device's effective local
    timezone).

    A slide is VISIBLE iff ALL configured constraints pass.  Any unset
    (NULL) constraint is "always open" for that dimension, so a row with
    no window columns set is always visible (byte-identical to the
    pre-feature behaviour).

    Constraints (see per-slide-visibility-design.md):
      * Date range ``[valid_from, valid_to]`` — both ends INCLUSIVE,
        floating local-calendar dates.
      * Weekday set ``active_days`` (0=Mon .. 6=Sun) — empty/None = all.
      * Time-of-day ``[active_start, active_end)`` — start INCLUSIVE,
        end EXCLUSIVE; ``active_start > active_end`` is an overnight
        wrap (e.g. 22:00..02:00).

    Wrap + weekday interaction: for the post-midnight tail of a wrapped
    window the effective weekday is *yesterday's* (the window "belongs"
    to the day it opened), so a Fri 22:00..02:00 slide is still open at
    Sat 00:30 when ``active_days`` lists Friday only.
    """
    d: date = local_now.date()
    t: time = local_now.time()

    # 1. Date range (inclusive both ends).
    if row.valid_from is not None and d < row.valid_from:
        return False
    if row.valid_to is not None and d > row.valid_to:
        return False

    start = row.active_start
    end = row.active_end

    # 2. Effective weekday — shift to yesterday for a wrapped window's tail.
    if start is not None and end is not None and start > end and t < end:
        effective_wd = (local_now - timedelta(days=1)).weekday()
    else:
        effective_wd = local_now.weekday()

    # 3. Weekday set.
    if row.active_days and effective_wd not in row.active_days:
        return False

    # 4. Time-of-day window.
    if start is not None and end is not None:
        if start < end:
            if not (start <= t < end):
                return False
        else:  # overnight wrap
            if not (t >= start or t < end):
                return False
    elif start is not None:
        if t < start:
            return False
    elif end is not None:
        if t >= end:
            return False

    return True


# Per-slide visibility window helpers (manifest schema 1.5) ------------------

#: An "all-None" window kwargs bundle, used on the Phase-1 server-drop path
#: so :class:`_SlideSpec` / :class:`_SlidePlan` carry no window metadata.
_EMPTY_WINDOW: dict = dict(
    valid_from=None,
    valid_to=None,
    active_days=None,
    active_start=None,
    active_end=None,
)


def _row_window_kwargs(row: SlideshowSlide) -> dict:
    """Extract a row's typed visibility-window columns as ``_SlideSpec``
    constructor kwargs.  ``active_days`` is normalised so a falsy value
    (``None`` or empty list) becomes ``None`` ("every day") — matching the
    CMS write-side normaliser and the firmware predicate.
    """
    return dict(
        valid_from=row.valid_from,
        valid_to=row.valid_to,
        active_days=(list(row.active_days) if row.active_days else None),
        active_start=row.active_start,
        active_end=row.active_end,
    )


def _window_wire_fields(p) -> dict:
    """Serialize a plan's typed window values to their wire form.

    Dates → ``"YYYY-MM-DD"``, times → ``"HH:MM:SS[.ffffff]"`` (full
    ``.isoformat()`` precision so the firmware's ``time.fromisoformat``
    round-trips verbatim — never truncate to ``HH:MM``).  ``active_days``
    is emitted as a plain ``list[int]`` (or ``None``).
    """
    ad = p.active_days
    return dict(
        valid_from=p.valid_from.isoformat() if p.valid_from is not None else None,
        valid_to=p.valid_to.isoformat() if p.valid_to is not None else None,
        active_days=(list(ad) if ad else None),
        active_start=p.active_start.isoformat() if p.active_start is not None else None,
        active_end=p.active_end.isoformat() if p.active_end is not None else None,
    )


async def _load_slide_specs(
    asset: Asset,
    db: AsyncSession,
    local_now: datetime | None = None,
    emit_windows: bool = False,
) -> list[_SlideSpec]:
    """Produce the ordered list of :class:`_SlideSpec` for ``asset``.

    The deck is a single ordered list of ``SlideshowSlide`` rows.  Each
    row is either a static ``asset`` slide (one spec, its own playback
    settings) or a dynamic ``tag`` block that expands in-place to current
    tag membership, every expanded member inheriting the row's playback
    columns as deck-defaults.  A deck with only ``asset`` rows behaves
    exactly as a classic manual slideshow.

    Per-slide visibility windows (manifest schema 1.5) have two paths:

    * ``emit_windows=False`` (default — devices without
      ``slideshow_visibility_v1`` and every non-device caller): closed
      slides are *dropped server-side* when ``local_now`` is supplied
      (Phase-1 behaviour); specs carry no window metadata.
    * ``emit_windows=True`` (capability devices): closed slides are
      *kept* and their window columns are copied onto every emitted spec
      (including each expanded tag member) so the device evaluates the
      window itself at exact local-clock boundaries, including offline.
    """
    rows_result = await db.execute(
        select(SlideshowSlide)
        .where(SlideshowSlide.slideshow_asset_id == asset.id)
        .order_by(SlideshowSlide.position.asc())
    )
    rows = rows_result.scalars().all()

    specs: list[_SlideSpec] = []
    pos = 0
    for row in rows:
        # Per-slide visibility window (schema 1.5).  On the server-drop
        # path, when a device-local now is supplied, drop rows whose window
        # is closed.  ``local_now is None`` (the API/builder readiness path)
        # means "show every slide" so the editor preview and existing tests
        # are unaffected.  Applies to both ``asset`` and ``tag`` rows; a
        # closed ``tag`` row skips its whole in-place expansion.  On the
        # capability path (``emit_windows``) closed rows are KEPT and their
        # window is carried onto each spec for device-side evaluation.
        if not emit_windows:
            if local_now is not None and not _slide_window_open(row, local_now):
                continue
        win = _row_window_kwargs(row) if emit_windows else _EMPTY_WINDOW
        if row.kind == "tag":
            if row.tag_id is None:  # defensive — CHECK constraint forbids
                continue
            member_ids = await expand_tag_members(row.tag_id, db)
            # Per-member transition: member 0 keeps the block's own
            # ``transition`` (the transition INTO the block, owned by the
            # timeline gap to its left).  Members 1..N use
            # ``member_transition`` — the "between items" control — falling
            # back to the block transition when it's NULL (the pre-feature
            # behaviour where every member shared one transition).
            for member_idx, aid in enumerate(member_ids):
                if member_idx == 0:
                    m_trans = row.transition
                    m_trans_ms = row.transition_ms
                else:
                    m_trans = (
                        row.member_transition
                        if row.member_transition is not None
                        else row.transition
                    )
                    m_trans_ms = (
                        row.member_transition_ms
                        if row.member_transition_ms is not None
                        else row.transition_ms
                    )
                specs.append(
                    _SlideSpec(
                        position=pos,
                        source_asset_id=aid,
                        duration_ms=row.duration_ms,
                        # Tag-block members share the block's single dwell
                        # time, but a VIDEO member plays to its natural end
                        # instead of being clipped to that dwell.  We don't
                        # know the member's asset type here (it's resolved
                        # later from ``sources_by_id``), so mark the spec as a
                        # tag member and let ``_plan_slides`` flip
                        # ``play_to_end`` on for videos.
                        play_to_end=False,
                        tag_member=True,
                        transition=m_trans,
                        transition_ms=m_trans_ms,
                        fit=row.fit,
                        effect=row.effect,
                        effect_direction=row.effect_direction,
                        **win,
                    )
                )
                pos += 1
        else:
            if row.source_asset_id is None:  # defensive — CHECK forbids
                continue
            specs.append(
                _SlideSpec(
                    position=pos,
                    source_asset_id=row.source_asset_id,
                    duration_ms=row.duration_ms,
                    play_to_end=row.play_to_end,
                    transition=row.transition,
                    transition_ms=row.transition_ms,
                    fit=row.fit,
                    effect=row.effect,
                    effect_direction=row.effect_direction,
                    **win,
                )
            )
            pos += 1
    return specs


async def expand_tag_members(
    tag_id: uuid.UUID, db: AsyncSession
) -> list[uuid.UUID]:
    """Return the ordered source-asset ids for a tag block.

    Members are every non-deleted, directly-displayable asset currently
    carrying ``tag_id``, ordered by ``asset_tags.created_at`` ascending
    (tagged-at order) so a newly tagged asset always sorts to the tail of
    its block — the firmware applies the new deck at a loop boundary so the
    insert is seamless.

    Shared with :func:`cms.composed.slideshow_expand.load_slideshow_members`
    so the device-resolve and composed-embed paths can't drift on tag-block
    membership/ordering.
    """
    result = await db.execute(
        select(Asset.id)
        .join(AssetTag, AssetTag.asset_id == Asset.id)
        .where(
            AssetTag.tag_id == tag_id,
            Asset.deleted_at.is_(None),
            Asset.asset_type.in_(_TAG_MEMBER_ASSET_TYPES),
        )
        .order_by(AssetTag.created_at.asc(), AssetTag.id.asc())
    )
    return list(result.scalars().all())


async def effective_slide_counts(
    slideshow_ids: Sequence[uuid.UUID], db: AsyncSession
) -> dict[uuid.UUID, int]:
    """Return the *effective* slide count for each slideshow asset id.

    A static ``asset`` slide counts as one.  A dynamic ``tag`` block counts
    as the number of assets it currently expands to (its live tag
    membership) — exactly what the device/preview renders — so a deck of
    "2 static + 1 tag" reports ``2 + N`` rather than ``3``.  An empty tag
    block (no current members) contributes ``0``, matching the resolver.

    Done in two batched queries (one for the slide rows, one grouped
    member count over every distinct tag referenced), so it stays O(1) in
    deck length and membership size regardless of how many slideshows are
    passed.
    """
    counts: dict[uuid.UUID, int] = {sid: 0 for sid in slideshow_ids}
    if not slideshow_ids:
        return counts

    rows = (
        await db.execute(
            select(
                SlideshowSlide.slideshow_asset_id,
                SlideshowSlide.kind,
                SlideshowSlide.tag_id,
            ).where(SlideshowSlide.slideshow_asset_id.in_(slideshow_ids))
        )
    ).all()

    # (slideshow_id, tag_id) for every tag block — kept per-row (not
    # de-duplicated) so two blocks pointing at the same tag both expand.
    tag_rows: list[tuple[uuid.UUID, uuid.UUID]] = []
    tag_ids: set[uuid.UUID] = set()
    for sid, kind, tag_id in rows:
        if kind == "tag":
            if tag_id is not None:
                tag_rows.append((sid, tag_id))
                tag_ids.add(tag_id)
        else:
            counts[sid] = counts.get(sid, 0) + 1

    if tag_ids:
        member_rows = (
            await db.execute(
                select(AssetTag.tag_id, func.count())
                .join(Asset, Asset.id == AssetTag.asset_id)
                .where(
                    AssetTag.tag_id.in_(tag_ids),
                    Asset.deleted_at.is_(None),
                    Asset.asset_type.in_(_TAG_MEMBER_ASSET_TYPES),
                )
                .group_by(AssetTag.tag_id)
            )
        ).all()
        member_counts = {tid: cnt for tid, cnt in member_rows}
        for sid, tag_id in tag_rows:
            counts[sid] = counts.get(sid, 0) + member_counts.get(tag_id, 0)

    return counts


async def plan_slideshow(
    asset: Asset,
    profile_id: Optional[uuid.UUID],
    db: AsyncSession,
    local_now: datetime | None = None,
    emit_windows: bool = False,
) -> SlideshowPlan:
    """Resolve every slide of ``asset`` against ``profile_id``.

    The slide list is a single ordered deck of ``slideshow_slides`` rows,
    transparently to the rest of this function (see
    :func:`_load_slide_specs`):

    * **static ``asset`` rows** — an explicit source asset with its own
      playback settings, ordered by ``position``.
    * **dynamic ``tag`` rows** (agora#806) — expand in-place to the set of
      assets currently carrying the row's tag, ordered by tag-membership
      creation time so a newly tagged asset always lands at the tail
      (Option B seamless insert).

    For each slide the planner picks the latest READY variant for the
    device's profile when one exists; otherwise falls back to the raw
    source asset; otherwise records a blocker.  Soft-deleted source
    assets and FAILED/CANCELLED variants are reported as blockers so the
    UI can surface what needs fixing.

    Two batched queries by source-asset-id keep this O(1) in slide count
    regardless of slideshow size.
    """
    specs = await _load_slide_specs(
        asset, db, local_now=local_now, emit_windows=emit_windows
    )
    if not specs:
        return SlideshowPlan()

    source_ids = [s.source_asset_id for s in specs]
    sources_result = await db.execute(
        select(Asset).where(Asset.id.in_(source_ids))
    )
    sources_by_id = {a.id: a for a in sources_result.scalars().all()}

    # Per-profile variant lookup: pick latest READY per source; also
    # collect the per-source latest non-deleted variant of any status so
    # we can report variant_processing / variant_failed / variant_cancelled
    # blockers when no READY exists.
    ready_by_source: dict[uuid.UUID, AssetVariant] = {}
    latest_any_by_source: dict[uuid.UUID, AssetVariant] = {}
    if profile_id is not None and source_ids:
        var_rows = (
            await db.execute(
                select(AssetVariant)
                .where(
                    AssetVariant.source_asset_id.in_(source_ids),
                    AssetVariant.profile_id == profile_id,
                    AssetVariant.deleted_at.is_(None),
                )
                # Deterministic tie-break on equal ``created_at`` — id DESC.
                .order_by(
                    AssetVariant.created_at.desc(),
                    AssetVariant.id.desc(),
                )
            )
        ).scalars().all()
        for v in var_rows:
            sid = v.source_asset_id
            if sid not in latest_any_by_source:
                latest_any_by_source[sid] = v
            if v.status == VariantStatus.READY and sid not in ready_by_source:
                ready_by_source[sid] = v

    plan = SlideshowPlan()
    for spec in specs:
        sid = spec.source_asset_id
        src = sources_by_id.get(sid)
        if src is None or src.deleted_at is not None:
            plan.blockers.append(
                SlideshowBlocker(
                    slide_position=spec.position,
                    source_asset_id=sid,
                    source_filename=src.filename if src else "",
                    status=BLOCKER_SOURCE_DELETED,
                )
            )
            continue
        sp = _SlidePlan(
            position=spec.position,
            source_asset_id=sid,
            source_filename=src.filename,
            source_asset_type=src.asset_type,
            duration_ms=spec.duration_ms,
            # A VIDEO member of a dynamic tag block plays its full natural
            # length rather than being clipped to the block's dwell time
            # (matches a standalone video slide with play_to_end on).  Other
            # member types (image, saved_stream, composed) keep the dwell.
            play_to_end=(
                spec.play_to_end
                or (spec.tag_member and src.asset_type == AssetType.VIDEO)
            ),
            transition=spec.transition,
            transition_ms=spec.transition_ms,
            fit=spec.fit,
            effect=spec.effect,
            effect_direction=spec.effect_direction,
            # Carry the per-slide visibility window through to the emitted
            # descriptor + resolved checksum.  None on the server-drop path
            # (emit_windows=False) → byte-identical wire + hash for devices
            # without ``slideshow_visibility_v1``.
            valid_from=spec.valid_from,
            valid_to=spec.valid_to,
            active_days=spec.active_days,
            active_start=spec.active_start,
            active_end=spec.active_end,
        )
        # File-asset slides need a download URL.  Saved streams are
        # behaviourally videos; treat them as such for variant lookup.
        if src.asset_type == AssetType.COMPOSED:
            # Composed member (Phase 5): the published bundle HTML *is*
            # the Asset's own file; there is no transcode variant.  Block
            # if never published (no bundle to download), then resolve the
            # referenced media as siblings the device pre-fetches.
            if composed_unpublished_reason(src) is not None:
                plan.blockers.append(
                    SlideshowBlocker(
                        slide_position=spec.position,
                        source_asset_id=sid,
                        source_filename=src.filename,
                        status=BLOCKER_SOURCE_UNPUBLISHED,
                    )
                )
                continue
            sib = await _plan_composed_siblings(src, profile_id, db)
            if sib == _SIBLINGS_INFLIGHT:
                plan.blockers.append(
                    SlideshowBlocker(
                        slide_position=spec.position,
                        source_asset_id=sid,
                        source_filename=src.filename,
                        status=BLOCKER_VARIANT_PROCESSING,
                    )
                )
                continue
            sp.download_path = src.filename
            sp.api_url_path = f"/api/assets/{src.id}/download"
            sp.checksum = src.checksum
            sp.size_bytes = src.size_bytes
            if sib:
                sp.siblings = sib  # type: ignore[assignment]
        elif profile_id is not None and src.asset_type in (
            AssetType.VIDEO,
            AssetType.IMAGE,
            AssetType.SAVED_STREAM,
        ):
            ready = ready_by_source.get(sid)
            if ready is not None:
                sp.download_path = f"variants/{ready.filename}"
                sp.api_url_path = f"/api/assets/variants/{ready.id}/download"
                sp.checksum = ready.checksum
                sp.size_bytes = ready.size_bytes
            else:
                latest = latest_any_by_source.get(sid)
                if latest is not None:
                    if latest.status in (
                        VariantStatus.PENDING,
                        VariantStatus.PROCESSING,
                    ):
                        status = BLOCKER_VARIANT_PROCESSING
                    elif latest.status == VariantStatus.FAILED:
                        status = BLOCKER_VARIANT_FAILED
                    else:
                        status = BLOCKER_VARIANT_CANCELLED
                    plan.blockers.append(
                        SlideshowBlocker(
                            slide_position=spec.position,
                            source_asset_id=sid,
                            source_filename=src.filename,
                            status=status,
                        )
                    )
                    continue
                # No variant exists for this profile — fall back to source.
                sp.download_path = src.filename
                sp.api_url_path = f"/api/assets/{src.id}/download"
                sp.checksum = src.checksum
                sp.size_bytes = src.size_bytes
        else:
            sp.download_path = src.filename
            sp.api_url_path = f"/api/assets/{src.id}/download"
            sp.checksum = src.checksum
            sp.size_bytes = src.size_bytes
        plan.slides.append(sp)
    return plan


def _compute_resolved_manifest_checksum(
    asset_checksum: str,
    slides: list[_SlidePlan],
    shuffle: bool = False,
    emit_windows: bool = False,
) -> str:
    """Per-device-profile resolved checksum.

    Folds the structural ``Asset.checksum`` together with each slide's
    selected variant checksum (or source checksum) so that re-transcoding
    a single source variant flips the manifest hash for any device on
    that profile, prompting a refetch.

    The deck-level ``shuffle`` bool is folded in so toggling it re-pushes
    the manifest.  The companion ``shuffle_seed`` is intentionally NOT
    folded — it's a stable function of the asset id so a re-fetch keeps
    the same per-cycle order without perturbing the checksum.

    Per-slide visibility windows (schema 1.5) are folded **only** when
    ``emit_windows`` is True (the capability path).  This keeps the digest
    byte-identical to the pre-1.5 behaviour for every device that doesn't
    advertise ``slideshow_visibility_v1`` — so bumping the resolver does
    NOT trigger a fleet-wide one-shot re-push.  On the capability path the
    *window definitions* (time-invariant) are folded, not their evaluated
    open/closed state, so the hash is stable across scheduler ticks and an
    author pushes the change exactly once.
    """
    h = hashlib.sha256()
    h.update((asset_checksum or "").encode())
    h.update(f"|shuffle={int(bool(shuffle))}".encode())
    for s in slides:
        h.update(
            f"|{s.position}|{s.source_asset_id}|{s.checksum or ''}|"
            f"{s.duration_ms}|{int(s.play_to_end)}|{s.transition}|{s.transition_ms}|"
            f"{s.fit}|{s.effect}|{s.effect_direction}".encode()
        )
        # Fold each composed sibling's checksum so a re-transcoded sibling
        # video flips the resolved hash and prompts a device refetch.
        # (Stricter than the standalone-composed path, which doesn't fold
        # siblings — intentional, avoids serving a stale cached bundle.)
        for sib in s.siblings:
            h.update(f"|sib|{sib.source_asset_id}|{sib.checksum or ''}".encode())
        if emit_windows:
            w = _window_wire_fields(s)
            days = ",".join(str(d) for d in (w["active_days"] or []))
            h.update(
                f"|w|{w['valid_from'] or ''}|{w['valid_to'] or ''}|{days}|"
                f"{w['active_start'] or ''}|{w['active_end'] or ''}".encode()
            )
    return h.hexdigest()


async def resolved_slideshow_checksum(
    asset: Asset,
    profile_id: Optional[uuid.UUID],
    db: AsyncSession,
    local_now: datetime | None = None,
    emit_windows: bool = False,
) -> Optional[str]:
    """Return the resolved manifest checksum for ``asset`` on a given profile.

    ``None`` if the slideshow isn't ready for the profile (any blockers).
    Used by :func:`build_device_sync` so ``ScheduleEntry.asset_checksum``
    and ``default_asset_checksum`` reflect per-profile variant choice.

    ``local_now`` (device effective local time) drives per-slide visibility
    windows.  With ``emit_windows=False`` (devices without
    ``slideshow_visibility_v1``) closed slides are dropped before the
    checksum is computed, so a slide opening/closing flips the resolved hash
    and triggers a one-shot device manifest refresh.  With
    ``emit_windows=True`` closed slides are KEPT and their time-invariant
    window definitions are folded, so the device evaluates the window itself
    and the hash only changes when an author edits the window.
    """
    plan = await plan_slideshow(
        asset, profile_id, db, local_now=local_now, emit_windows=emit_windows
    )
    if not plan.ready:
        return None
    return _compute_resolved_manifest_checksum(
        asset.checksum or "", plan.slides, bool(asset.shuffle), emit_windows
    )


async def device_local_now(device: Device, db: AsyncSession) -> datetime:
    """Return the current time in ``device``'s effective local timezone.

    A per-device IANA tz override takes precedence over the CMS global
    ``timezone`` setting (mirroring the wire ``timezone`` the device
    applies locally); falls back to UTC when neither is set or the value
    is invalid.  Drives per-slide visibility-window evaluation in
    :func:`build_fetch_for_slideshow` / :func:`resolved_slideshow_checksum`.
    """
    from cms.models.setting import CMSSetting  # lazy: avoid import cycle

    tz_name = getattr(device, "timezone", None)
    if not tz_name:
        row = await db.execute(
            select(CMSSetting.value).where(CMSSetting.key == "timezone")
        )
        tz_name = row.scalar_one_or_none() or "UTC"
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")
    return datetime.now(timezone.utc).astimezone(tz)


async def build_fetch_for_slideshow(
    asset: Asset,
    device: Device,
    base_url: str,
    db: AsyncSession,
    local_now: datetime | None = None,
    emit_windows: bool = False,
) -> Optional[FetchAssetMessage]:
    """Build a slideshow ``FetchAssetMessage`` for ``device``.

    Returns ``None`` if the slideshow isn't ready (any blockers) — same
    contract as the existing video/image resolver.

    ``local_now`` (device effective local time) drives per-slide visibility
    windows.  With ``emit_windows=False`` closed slides are dropped from the
    emitted manifest (server-side evaluation).  With ``emit_windows=True``
    (devices advertising ``slideshow_visibility_v1``) closed slides are KEPT
    and their window metadata is carried on each ``SlideDescriptor`` so the
    device evaluates the window itself at exact local-clock boundaries.
    """
    plan = await plan_slideshow(
        asset, device.profile_id, db, local_now=local_now, emit_windows=emit_windows
    )
    if not plan.ready:
        return None

    storage = get_storage()
    descriptors: list[SlideDescriptor] = []
    for sp in plan.slides:
        api_url = f"{base_url}{sp.api_url_path}"
        download_url = await storage.get_device_download_url(
            sp.download_path or "", api_url
        )
        # Pick the wire asset_type.  Composed members ship the bundle HTML
        # plus their referenced media as siblings.
        if sp.source_asset_type == AssetType.COMPOSED:
            wire_type = "composed"
        elif sp.source_asset_type in (AssetType.VIDEO, AssetType.SAVED_STREAM):
            wire_type = "video"
        else:
            wire_type = "image"

        wire_siblings: Optional[list[Sibling]] = None
        if sp.siblings:
            wire_siblings = []
            for sib in sp.siblings:
                sib_api_url = f"{base_url}{sib.api_url_path}"
                sib_download_url = await storage.get_device_download_url(
                    sib.download_path, sib_api_url
                )
                wire_siblings.append(
                    Sibling(
                        name=sib.filename,
                        asset_type=sib.asset_type_value,
                        download_url=sib_download_url,
                        checksum=sib.checksum,
                        size_bytes=sib.size_bytes,
                    )
                )

        win = _window_wire_fields(sp) if emit_windows else _EMPTY_WINDOW
        descriptors.append(
            SlideDescriptor(
                asset_name=sp.source_filename,
                asset_type=wire_type,
                download_url=download_url,
                checksum=sp.checksum or "",
                size_bytes=sp.size_bytes or 0,
                duration_ms=sp.duration_ms,
                play_to_end=sp.play_to_end,
                transition=sp.transition,
                transition_ms=sp.transition_ms,
                fit=sp.fit,
                effect=sp.effect,
                effect_direction=sp.effect_direction,
                siblings=wire_siblings,
                valid_from=win["valid_from"],
                valid_to=win["valid_to"],
                active_days=win["active_days"],
                active_start=win["active_start"],
                active_end=win["active_end"],
            )
        )

    resolved = _compute_resolved_manifest_checksum(
        asset.checksum or "", plan.slides, bool(asset.shuffle), emit_windows
    )

    # Phase 1b of agora#226 — wall-clock anchor support.
    cycle_duration_ms = sum(sp.duration_ms for sp in plan.slides)
    # Tag-bearing decks (agora#806, Option B) carry a persisted anchor on
    # ``asset.slideshow_anchor_at`` that is set once a tag block first
    # appears and NEVER re-floored, so appending a newly-tagged slide at
    # the tail leaves every existing slide's cycle offset unchanged — the
    # on-screen slide does not move.  Plain manual decks (anchor NULL) fall
    # back to flooring ``now`` to a cycle boundary on every build.
    if asset.slideshow_anchor_at is not None:
        started_at = _format_anchor(asset.slideshow_anchor_at)
    else:
        started_at = _compute_cycle_anchor(cycle_duration_ms)

    # Deck-level shuffle (agora#261).  Emit the bool + a stable per-asset
    # seed so every device derives the same per-cycle permutation and a
    # re-fetch keeps the same order (seed does not perturb the checksum).
    shuffle = bool(asset.shuffle)
    shuffle_seed = _shuffle_seed_for_asset(asset.id) if shuffle else None

    return FetchAssetMessage(
        asset_name=asset.filename,
        download_url="",
        checksum=resolved,
        size_bytes=0,
        asset_type=AssetType.SLIDESHOW.value,
        slides=descriptors,
        manifest_schema_version=SLIDESHOW_MANIFEST_SCHEMA_VERSION_LATEST,
        cycle_duration_ms=cycle_duration_ms,
        started_at=started_at,
        shuffle=shuffle,
        shuffle_seed=shuffle_seed,
    )


def _shuffle_seed_for_asset(asset_id: uuid.UUID) -> int:
    """Stable, process-independent shuffle seed derived from the asset id.

    Uses SHA-256 (not Python's salted ``hash()``) so the value is
    identical across CMS processes, restarts, and re-fetches — the device
    needs the same seed every time to keep a consistent per-cycle order.
    Truncated to a 31-bit non-negative int (fits a signed 32-bit / JS
    safe integer range comfortably).
    """
    digest = hashlib.sha256(asset_id.bytes).digest()
    return int.from_bytes(digest[:4], "big") & 0x7FFFFFFF


def _compute_cycle_anchor(cycle_duration_ms: int) -> Optional[str]:
    """Floor ``now_utc`` to the nearest ``cycle_duration_ms`` boundary
    since the Unix epoch, returned as an ISO-8601 UTC string.

    The anchor is purely derived from the wall clock and the cycle
    length, so any two devices computing it locally with synchronized
    clocks would land on the same instant — which is what makes the
    chromium-player branch's "same content at the same second" promise
    work across reflashes and reboots.

    Returns ``None`` if ``cycle_duration_ms <= 0`` (defensive — an empty
    deck shouldn't reach here because the planner rejects it earlier).
    """
    if cycle_duration_ms <= 0:
        return None
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    floor_ms = (now_ms // cycle_duration_ms) * cycle_duration_ms
    anchor = datetime.fromtimestamp(floor_ms / 1000, tz=timezone.utc)
    # ISO-8601 with millisecond precision, "Z" suffix for UTC.
    iso = anchor.isoformat(timespec="milliseconds")
    return iso.replace("+00:00", "Z")


def _format_anchor(dt: datetime) -> str:
    """Format a UTC ``datetime`` as the same ISO-8601 ``Z`` string the
    cycle-anchor helper emits, so a persisted tag-rule anchor is wire
    identical to a computed one.  Naive datetimes are assumed UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    iso = dt.isoformat(timespec="milliseconds")
    return iso.replace("+00:00", "Z")


async def slideshow_readiness(
    asset: Asset, profile_id: Optional[uuid.UUID], db: AsyncSession
) -> dict:
    """JSON-friendly readiness summary used by the API + builder UI."""
    plan = await plan_slideshow(asset, profile_id, db)
    return {
        "ready": plan.ready,
        "blockers": [
            {
                "slide_position": b.slide_position,
                "source_asset_id": str(b.source_asset_id),
                "source_filename": b.source_filename,
                "status": b.status,
            }
            for b in plan.blockers
        ],
    }
