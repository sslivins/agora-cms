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
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cms.models.asset import Asset, AssetType, AssetVariant, VariantStatus
from cms.models.device import Device
from cms.schemas.protocol import FetchAssetMessage, SlideDescriptor
from cms.services.storage import get_storage
from shared.models.slideshow_slide import SlideshowSlide


# Blocker statuses returned by the planner — exposed via API so the UI
# can render an actionable message per slide.
BLOCKER_SOURCE_DELETED = "source_deleted"
BLOCKER_VARIANT_PROCESSING = "variant_processing"
BLOCKER_VARIANT_FAILED = "variant_failed"
BLOCKER_VARIANT_CANCELLED = "variant_cancelled"


@dataclass
class _SlidePlan:
    """One resolved slide entry — internal to this module."""

    position: int
    source_asset_id: uuid.UUID
    source_filename: str
    source_asset_type: AssetType
    duration_ms: int
    play_to_end: bool
    # Populated for ready slides only:
    download_path: Optional[str] = None  # storage path for get_device_download_url
    api_url_path: Optional[str] = None  # CMS-relative API URL for fallback
    checksum: Optional[str] = None
    size_bytes: Optional[int] = None


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
        return not self.blockers


async def plan_slideshow(
    asset: Asset, profile_id: Optional[uuid.UUID], db: AsyncSession
) -> SlideshowPlan:
    """Resolve every slide of ``asset`` against ``profile_id``.

    For each slide the planner picks the latest READY variant for the
    device's profile when one exists; otherwise falls back to the raw
    source asset; otherwise records a blocker.  Soft-deleted source
    assets and FAILED/CANCELLED variants are reported as blockers so the
    UI can surface what needs fixing.

    Two batched queries by source-asset-id keep this O(1) in slide count
    regardless of slideshow size.
    """
    rows = (
        await db.execute(
            select(SlideshowSlide)
            .where(SlideshowSlide.slideshow_asset_id == asset.id)
            .order_by(SlideshowSlide.position.asc())
        )
    ).scalars().all()
    if not rows:
        return SlideshowPlan()

    source_ids = [r.source_asset_id for r in rows]
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
    for slide_row in rows:
        sid = slide_row.source_asset_id
        src = sources_by_id.get(sid)
        if src is None or src.deleted_at is not None:
            plan.blockers.append(
                SlideshowBlocker(
                    slide_position=slide_row.position,
                    source_asset_id=sid,
                    source_filename=src.filename if src else "",
                    status=BLOCKER_SOURCE_DELETED,
                )
            )
            continue
        sp = _SlidePlan(
            position=slide_row.position,
            source_asset_id=sid,
            source_filename=src.filename,
            source_asset_type=src.asset_type,
            duration_ms=slide_row.duration_ms,
            play_to_end=slide_row.play_to_end,
        )
        # File-asset slides need a download URL.  Saved streams are
        # behaviourally videos; treat them as such for variant lookup.
        if profile_id is not None and src.asset_type in (
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
                            slide_position=slide_row.position,
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
    asset_checksum: str, slides: list[_SlidePlan]
) -> str:
    """Per-device-profile resolved checksum.

    Folds the structural ``Asset.checksum`` together with each slide's
    selected variant checksum (or source checksum) so that re-transcoding
    a single source variant flips the manifest hash for any device on
    that profile, prompting a refetch.
    """
    h = hashlib.sha256()
    h.update((asset_checksum or "").encode())
    for s in slides:
        h.update(
            f"|{s.position}|{s.source_asset_id}|{s.checksum or ''}|"
            f"{s.duration_ms}|{int(s.play_to_end)}".encode()
        )
    return h.hexdigest()


async def resolved_slideshow_checksum(
    asset: Asset, profile_id: Optional[uuid.UUID], db: AsyncSession
) -> Optional[str]:
    """Return the resolved manifest checksum for ``asset`` on a given profile.

    ``None`` if the slideshow isn't ready for the profile (any blockers).
    Used by :func:`build_device_sync` so ``ScheduleEntry.asset_checksum``
    and ``default_asset_checksum`` reflect per-profile variant choice.
    """
    plan = await plan_slideshow(asset, profile_id, db)
    if not plan.ready:
        return None
    return _compute_resolved_manifest_checksum(asset.checksum or "", plan.slides)


async def build_fetch_for_slideshow(
    asset: Asset,
    device: Device,
    base_url: str,
    db: AsyncSession,
) -> Optional[FetchAssetMessage]:
    """Build a slideshow ``FetchAssetMessage`` for ``device``.

    Returns ``None`` if the slideshow isn't ready (any blockers) — same
    contract as the existing video/image resolver.
    """
    plan = await plan_slideshow(asset, device.profile_id, db)
    if not plan.ready:
        return None

    storage = get_storage()
    descriptors: list[SlideDescriptor] = []
    for sp in plan.slides:
        api_url = f"{base_url}{sp.api_url_path}"
        download_url = await storage.get_device_download_url(
            sp.download_path or "", api_url
        )
        descriptors.append(
            SlideDescriptor(
                asset_name=sp.source_filename,
                asset_type=(
                    "video"
                    if sp.source_asset_type
                    in (AssetType.VIDEO, AssetType.SAVED_STREAM)
                    else "image"
                ),
                download_url=download_url,
                checksum=sp.checksum or "",
                size_bytes=sp.size_bytes or 0,
                duration_ms=sp.duration_ms,
                play_to_end=sp.play_to_end,
            )
        )

    resolved = _compute_resolved_manifest_checksum(asset.checksum or "", plan.slides)
    return FetchAssetMessage(
        asset_name=asset.filename,
        download_url="",
        checksum=resolved,
        size_bytes=0,
        asset_type=AssetType.SLIDESHOW.value,
        slides=descriptors,
    )


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
