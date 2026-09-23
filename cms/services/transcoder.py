"""Transcoder service — CMS-side shim.

Transcoding runs in the dedicated worker container (``worker/``).
This module retains:
  - DB helpers that create pending ``AssetVariant`` rows (called from
    CMS routers + the startup defaults hook).
  - ``enqueue_variants`` / ``enqueue_stream_capture`` — the CMS-facing
    API for queueing work; both create Job rows and send queue messages
    via :mod:`shared.services.jobs`.
  - ``stream_capture_monitor_loop`` — reconciles completed captures →
    variant rows + sweeps orphan jobs.
  - No-op cancel stubs (the worker handles its own cancellation).
"""

import asyncio
import logging
import os
import uuid
from typing import Iterable

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.asset import Asset, AssetType, AssetVariant, VariantStatus
from shared.models.device_profile import DeviceProfile
from shared.models.job import JobType
from shared.services.image import convert_image, convert_image_to_jpeg, image_variant_ext  # noqa: F401
from shared.services.jobs import (
    drain_outbox,
    enqueue_job,
    enqueue_jobs,
)
from shared.services.probe import probe_media  # noqa: F401

logger = logging.getLogger("agora.cms.transcoder")


# Profile fields that actually affect image-variant output. ``convert_image``
# only honours ``max_width``/``max_height``; codec/bitrate/crf/fps/audio/
# pixel-format/color-space are all video-only knobs.  A profile edit that
# touches only those video-only fields must NOT re-encode images — see
# issue #302-ish (image-supersede-over-eager).
IMAGE_RELEVANT_FIELDS: frozenset[str] = frozenset({"max_width", "max_height"})

# Asset types that have no source media file and whose ONLY meaningful
# variant is a rendered thumbnail snapshot:
#   - COMPOSED slides are snapshotted from their generated bundle HTML.
#   - WEBPAGE assets are snapshotted by navigating headless Chromium to
#     their live URL.
# Both must be restricted to thumbnail-purpose profiles — a device-purpose
# profile would try to ffmpeg a non-existent source file and emit a useless
# ``.mp4``.
THUMBNAIL_ONLY_ASSET_TYPES: tuple[AssetType, ...] = (
    AssetType.COMPOSED,
    AssetType.WEBPAGE,
)

# ── Monitor loop intervals ──────────────────────────────────────
_MONITOR_INTERVAL = int(os.environ.get("AGORA_MONITOR_INTERVAL", "30"))
# How long a PROCESSING job may go without a heartbeat before the monitor
# treats its worker as dead.  The worker stamps ``Job.heartbeat_at`` every
# HEARTBEAT_INTERVAL (15s), so this is several cycles of slack — enough to
# ride out a transient DB blip without reaping a live job.
#
# This is a *liveness* timeout, not a duration budget.  A transcode may run
# for hours and stay healthy so long as it keeps heartbeating.
_STALE_HEARTBEAT_TIMEOUT = int(os.environ.get("AGORA_STALE_HEARTBEAT_TIMEOUT", "120"))
# Cap on how many stale variants one tick will recover, so a mass worker
# outage can't turn a single tick into an unbounded rewrite.
_STALE_RESET_BATCH = int(os.environ.get("AGORA_STALE_RESET_BATCH", "50"))
# Outbox drainer interval — kept short so producer-to-queue latency stays
# sub-second-ish.  In Postgres mode we additionally LISTEN on the
# ``transcode_outbox`` channel for instant wake-up; this poll is the
# safety net that catches missed NOTIFYs (e.g. CMS crashed after commit
# but before NOTIFY, or LISTEN connection dropped silently).
_OUTBOX_DRAIN_INTERVAL = int(os.environ.get("AGORA_OUTBOX_DRAIN_INTERVAL", "5"))


def _image_variant_ext(asset) -> str:
    """Return the correct file extension for an image variant."""
    return image_variant_ext(asset.filename)


def _variant_ext_for(asset: Asset, profile: DeviceProfile) -> str:
    """Pick the correct file extension for a new variant of ``asset``
    under ``profile``.

    Thumbnail-purpose profiles always emit ``.jpg`` regardless of
    source type — the variant is a single still frame, never video.
    Otherwise we fall back to the legacy per-asset-type rules:
    images keep their natural extension (``.png`` for PNG sources,
    ``.jpg`` otherwise); audio-only profiles use ``.mkv``; everything
    else is ``.mp4``.
    """
    if getattr(profile, "purpose", "device") == "thumbnail":
        return ".jpg"
    if asset.asset_type == AssetType.IMAGE:
        return image_variant_ext(asset.filename)
    if profile.audio_codec == "libopus":
        return ".mkv"
    return ".mp4"


def _profile_emits_for_asset(profile: DeviceProfile, asset: Asset) -> bool:
    """Whether ``profile`` should produce a variant for ``asset``.

    Composed slides and webpage assets have no source media file — the
    only meaningful variant for them is a thumbnail snapshot the worker
    renders (from the slide HTML, or by navigating to the webpage URL). A
    device-purpose profile would try to ffmpeg a non-existent source and
    emit a useless ``.mp4``, so these asset types are restricted to
    thumbnail-purpose profiles. All other asset types are emitted by every
    profile as before.
    """
    if asset.asset_type in THUMBNAIL_ONLY_ASSET_TYPES:
        return getattr(profile, "purpose", "device") == "thumbnail"
    return True


def cancel_profile_transcodes(profile_id: uuid.UUID) -> bool:
    """No-op — transcoding runs in the worker container."""
    return False


def cancel_asset_transcodes(asset_id: uuid.UUID) -> bool:
    """No-op — transcoding runs in the worker container."""
    return False


async def flag_profile_jobs_cancelled(
    db: AsyncSession, profile_id: uuid.UUID
) -> int:
    """Set ``cancel_requested = True`` on all active VARIANT_TRANSCODE jobs
    whose target variant belongs to ``profile_id``.

    Caller is responsible for committing the surrounding transaction.
    Returns the number of jobs flagged.  The worker heartbeat picks up the
    flag within ~15s and SIGTERMs the child ffmpeg.
    """
    from sqlalchemy import update
    from shared.models.job import Job, JobStatus

    variant_ids_subq = (
        select(AssetVariant.id).where(AssetVariant.profile_id == profile_id)
    ).scalar_subquery()

    result = await db.execute(
        update(Job)
        .where(
            Job.status.in_([JobStatus.PENDING, JobStatus.PROCESSING]),
            Job.type == JobType.VARIANT_TRANSCODE,
            Job.target_id.in_(variant_ids_subq),
        )
        .values(cancel_requested=True)
    )
    flagged = result.rowcount or 0
    if flagged:
        logger.info(
            "Flagged %d active VARIANT_TRANSCODE job(s) cancel_requested=True "
            "for profile %s", flagged, profile_id,
        )
    return flagged


async def supersede_profile_variants(
    db: AsyncSession,
    profile_id: uuid.UUID,
    changed_fields: Iterable[str] | None = None,
) -> list[uuid.UUID]:
    """Create fresh PENDING variant rows for every source asset that currently
    has a non-deleted variant under ``profile_id``.

    Part of the "latest-READY-wins" profile-change flow: old variant rows
    are LEFT IN PLACE (still READY/PROCESSING/whatever) so devices keep
    playing the last good blob while the new transcode runs.  When the new
    variant reaches READY, the reaper supersession sweep will soft-delete
    the older sibling(s); once their jobs are terminal it hard-deletes
    them (blob + row).

    ``changed_fields`` is the set of profile fields that triggered this
    supersession.  When supplied and it intersects none of
    :data:`IMAGE_RELEVANT_FIELDS`, IMAGE assets are left alone — there is
    nothing about the changed fields (e.g. video codec/bitrate/crf) that
    would affect their rendered output, so re-encoding them is pure
    wasted work.  When omitted (or when a dimension field changes),
    images are superseded alongside videos, matching the legacy behaviour.

    Returns the list of newly-created variant ids (caller passes these to
    :func:`enqueue_variants`).  Caller is responsible for committing the
    surrounding transaction.
    """
    changed_set = set(changed_fields) if changed_fields is not None else None
    skip_images = (
        changed_set is not None
        and not (changed_set & IMAGE_RELEVANT_FIELDS)
    )
    profile_result = await db.execute(
        select(DeviceProfile).where(DeviceProfile.id == profile_id)
    )
    profile = profile_result.scalar_one_or_none()
    if profile is None:
        logger.warning(
            "supersede_profile_variants: profile %s not found", profile_id
        )
        return []

    # Find distinct source assets that currently have a non-deleted variant
    # for this profile — those are the ones we need to re-transcode.
    asset_rows = (
        await db.execute(
            select(AssetVariant.source_asset_id)
            .where(
                AssetVariant.profile_id == profile_id,
                AssetVariant.deleted_at.is_(None),
            )
            .distinct()
        )
    ).all()
    source_asset_ids = [row[0] for row in asset_rows]

    if not source_asset_ids:
        logger.info(
            "supersede_profile_variants: profile %s has no live variants to "
            "supersede", profile_id,
        )
        return []

    # Load the assets so we can pick the correct filename extension.
    assets_result = await db.execute(
        select(Asset).where(Asset.id.in_(source_asset_ids))
    )
    assets = {a.id: a for a in assets_result.scalars().all()}

    new_variant_ids: list[uuid.UUID] = []
    for asset_id in source_asset_ids:
        asset = assets.get(asset_id)
        if asset is None:
            logger.warning(
                "supersede_profile_variants: source asset %s missing; "
                "skipping supersession for profile %s",
                asset_id, profile_id,
            )
            continue
        # Skip soft-deleted assets — the asset reaper will clean up their
        # variants shortly anyway.
        if getattr(asset, "deleted_at", None) is not None:
            logger.info(
                "supersede_profile_variants: skipping soft-deleted asset %s "
                "for profile %s", asset_id, profile_id,
            )
            continue

        # Image variants only depend on max_width/max_height; skip them
        # when the profile edit doesn't touch either of those fields.
        if skip_images and asset.asset_type == AssetType.IMAGE:
            logger.info(
                "supersede_profile_variants: skipping IMAGE asset %s for "
                "profile %s — changed fields %s do not affect image output",
                asset_id, profile_id, sorted(changed_set),
            )
            continue

        variant_id = uuid.uuid4()
        ext = _variant_ext_for(asset, profile)
        db.add(
            AssetVariant(
                id=variant_id,
                source_asset_id=asset.id,
                profile_id=profile_id,
                filename=f"{variant_id}{ext}",
                status=VariantStatus.PENDING,
            )
        )
        new_variant_ids.append(variant_id)

    logger.info(
        "supersede_profile_variants: created %d fresh PENDING variant(s) for "
        "profile %s (new variant ids=%s)",
        len(new_variant_ids), profile_id,
        [str(v) for v in new_variant_ids[:10]],
    )
    return new_variant_ids


# ── Job enqueue helpers (CMS-facing API) ────────────────────────

async def enqueue_variants(
    db: AsyncSession, variant_ids: Iterable[uuid.UUID]
) -> list[uuid.UUID]:
    """Enqueue one VARIANT_TRANSCODE job per variant id.

    Returns the list of job ids created.  Safe to call with an empty
    iterable (no-op).
    """
    specs = [(JobType.VARIANT_TRANSCODE, vid) for vid in variant_ids]
    if not specs:
        return []
    return await enqueue_jobs(db, specs)


async def enqueue_stream_capture(
    db: AsyncSession, asset_id: uuid.UUID
) -> uuid.UUID:
    """Enqueue a STREAM_CAPTURE job for the given SAVED_STREAM asset."""
    return await enqueue_job(db, JobType.STREAM_CAPTURE, asset_id)


async def notify_worker(db, count: int = 1) -> None:
    """Compatibility shim — wake the worker via PostgreSQL NOTIFY.

    New code should call :func:`enqueue_variants` or
    :func:`enqueue_stream_capture` instead (they send proper queue
    messages).  This shim only issues ``NOTIFY transcode_jobs`` so
    listen-mode workers running in docker-compose wake up on demand.
    """
    from shared.services.jobs import _notify_pg
    await _notify_pg(db)


# ── Variant creation helpers ────────────────────────────────────

async def enqueue_for_new_profile(
    profile_id, db: AsyncSession
) -> list[uuid.UUID]:
    """Create pending variants for all applicable assets for a new profile.

    Device-purpose profiles cover VIDEO + IMAGE assets (as before). A
    thumbnail-purpose profile additionally covers COMPOSED slides, whose
    only meaningful variant is the rendered snapshot — but a *device*
    profile must never enqueue a composed slide (it has no source file
    and would emit a useless ``.mp4``).

    Returns the list of newly-created variant ids (caller may pass these to
    :func:`enqueue_variants`).
    """
    profile_result = await db.execute(
        select(DeviceProfile).where(DeviceProfile.id == profile_id)
    )
    profile = profile_result.scalar_one_or_none()
    if not profile:
        return []
    # Skip disabled profiles — they don't generate new variants.
    if not getattr(profile, "enabled", True):
        logger.info(
            "enqueue_for_new_profile: profile %s (%s) is disabled — skipping",
            profile_id, profile.name,
        )
        return []

    wanted_types = [AssetType.VIDEO, AssetType.IMAGE]
    if getattr(profile, "purpose", "device") == "thumbnail":
        wanted_types.extend(THUMBNAIL_ONLY_ASSET_TYPES)

    result = await db.execute(
        select(Asset).where(
            Asset.asset_type.in_(wanted_types),
            Asset.deleted_at.is_(None),
        )
    )
    assets = result.scalars().all()

    new_variant_ids: list[uuid.UUID] = []
    for asset in assets:
        existing = await db.execute(
            select(AssetVariant.id).where(
                AssetVariant.source_asset_id == asset.id,
                AssetVariant.profile_id == profile_id,
                AssetVariant.deleted_at.is_(None),
            ).limit(1)
        )
        # The latest-READY-wins thumbnail flow can leave more than one
        # non-deleted variant per (asset, profile); we only care whether
        # *any* already exists, so never use scalar_one_or_none here
        # (it raises MultipleResultsFound and crashed startup seeding).
        if existing.first() is not None:
            continue

        variant_id = uuid.uuid4()
        ext = _variant_ext_for(asset, profile)
        variant = AssetVariant(
            id=variant_id,
            source_asset_id=asset.id,
            profile_id=profile_id,
            filename=f"{variant_id}{ext}",
        )
        db.add(variant)
        new_variant_ids.append(variant_id)

    await db.commit()
    return new_variant_ids


async def enqueue_composed_thumbnail(
    asset: Asset, db: AsyncSession
) -> list[uuid.UUID]:
    """Queue a fresh snapshot render for a thumbnail-only asset.

    Handles both composed slides (snapshotted from the bundle HTML) and
    webpage assets (snapshotted from the live URL) — any asset type in
    :data:`THUMBNAIL_ONLY_ASSET_TYPES`. Other asset types are a no-op.

    Mirrors the latest-READY-wins flow used for profile changes: any
    existing thumbnail variant rows are left in place (the grid keeps
    showing the previous snapshot) while a new PENDING variant is added
    per enabled thumbnail-purpose profile. The worker renders the snapshot
    to a JPEG; when the new variant reaches READY the reaper sweep
    supersedes the older sibling.

    Self-contained: creates the variant rows, commits, and enqueues the
    transcode jobs. Designed to be called best-effort from the layout
    save hook / webpage create+URL-edit hooks (caller wraps in try/except
    so a transient failure never blocks a save). Returns the new variant
    ids.
    """
    if asset.asset_type not in THUMBNAIL_ONLY_ASSET_TYPES:
        return []

    profiles = (
        await db.execute(
            select(DeviceProfile).where(DeviceProfile.purpose == "thumbnail")
        )
    ).scalars().all()
    enabled = [p for p in profiles if getattr(p, "enabled", True)]
    if not enabled:
        return []

    # Coalesce rapid saves: if a render is already PENDING for this slide
    # under a profile, don't pile on another — the queued job reads the
    # current layout at render time, so it will already snapshot the latest
    # save. Profiles whose only variants are PROCESSING/READY/older still get
    # a fresh PENDING (the in-flight one may have captured stale HTML).
    pending_profile_ids = set(
        (
            await db.execute(
                select(AssetVariant.profile_id).where(
                    AssetVariant.source_asset_id == asset.id,
                    AssetVariant.profile_id.in_([p.id for p in enabled]),
                    AssetVariant.status == VariantStatus.PENDING,
                    AssetVariant.deleted_at.is_(None),
                )
            )
        ).scalars().all()
    )

    new_variant_ids: list[uuid.UUID] = []
    for profile in enabled:
        if profile.id in pending_profile_ids:
            continue
        variant_id = uuid.uuid4()
        ext = _variant_ext_for(asset, profile)
        db.add(
            AssetVariant(
                id=variant_id,
                source_asset_id=asset.id,
                profile_id=profile.id,
                filename=f"{variant_id}{ext}",
                status=VariantStatus.PENDING,
            )
        )
        new_variant_ids.append(variant_id)

    if new_variant_ids:
        # enqueue_variants -> enqueue_jobs commits the new variant rows and
        # their job/outbox rows in one transaction, so a variant is never
        # left PENDING without a job to render it.
        await enqueue_variants(db, new_variant_ids)
        logger.info(
            "enqueue_composed_thumbnail: queued %d snapshot render(s) for "
            "%s asset %s", len(new_variant_ids), asset.asset_type.value,
            asset.id,
        )
    return new_variant_ids


# Clearer generic alias — both composed slides and webpage assets enqueue
# the same thumbnail snapshot render. New callers (webpage create / URL-edit
# hooks) should prefer this name.
enqueue_thumbnail = enqueue_composed_thumbnail


async def enqueue_missing_composed_thumbnails(db: AsyncSession) -> int:
    """Idempotent startup backfill: ensure every thumbnail-only asset has one.

    For each non-deleted asset whose type is in
    :data:`THUMBNAIL_ONLY_ASSET_TYPES` (composed slides + webpage assets)
    that has no live thumbnail variant (PENDING / PROCESSING / READY)
    under any enabled thumbnail-purpose profile, enqueue a fresh snapshot
    render. Assets that already have one — or have one in flight — are
    skipped, so this is safe to run on every boot. Returns the number of
    assets for which a render was enqueued.
    """
    profiles = (
        await db.execute(
            select(DeviceProfile).where(DeviceProfile.purpose == "thumbnail")
        )
    ).scalars().all()
    enabled_profiles = [p for p in profiles if getattr(p, "enabled", True)]
    if not enabled_profiles:
        return 0
    enabled_ids = [p.id for p in enabled_profiles]

    assets = (
        await db.execute(
            select(Asset).where(
                Asset.asset_type.in_(THUMBNAIL_ONLY_ASSET_TYPES),
                Asset.deleted_at.is_(None),
            )
        )
    ).scalars().all()
    if not assets:
        return 0

    new_variant_ids: list[uuid.UUID] = []
    enqueued = 0
    for asset in assets:
        # Only a *live* variant (queued, rendering, or done) counts as
        # "already has a thumbnail". A FAILED/CANCELLED snapshot must not
        # block a fresh render on the next boot.
        existing = await db.execute(
            select(AssetVariant.id).where(
                AssetVariant.source_asset_id == asset.id,
                AssetVariant.profile_id.in_(enabled_ids),
                AssetVariant.status.in_(
                    [
                        VariantStatus.PENDING,
                        VariantStatus.PROCESSING,
                        VariantStatus.READY,
                    ]
                ),
                AssetVariant.deleted_at.is_(None),
            ).limit(1)
        )
        if existing.scalar_one_or_none() is not None:
            continue
        for profile in enabled_profiles:
            variant_id = uuid.uuid4()
            ext = _variant_ext_for(asset, profile)
            db.add(
                AssetVariant(
                    id=variant_id,
                    source_asset_id=asset.id,
                    profile_id=profile.id,
                    filename=f"{variant_id}{ext}",
                    status=VariantStatus.PENDING,
                )
            )
            new_variant_ids.append(variant_id)
        enqueued += 1

    if new_variant_ids:
        # enqueue_variants commits the variant + job/outbox rows atomically.
        await enqueue_variants(db, new_variant_ids)
        logger.info(
            "enqueue_missing_composed_thumbnails: enqueued snapshot render(s) "
            "for %d asset(s)", enqueued,
        )
    return enqueued


# Clearer generic alias for the backfill (covers composed + webpage).
enqueue_missing_thumbnails = enqueue_missing_composed_thumbnails


async def fix_image_variant_extensions(db: AsyncSession) -> int:
    """Fix image variants with incorrect .mp4 extensions.

    Resets them to PENDING with the correct extension so the worker
    re-processes them.  Returns the number of variants fixed.
    """
    result = await db.execute(
        select(AssetVariant).join(Asset, AssetVariant.source_asset_id == Asset.id).where(
            Asset.asset_type == AssetType.IMAGE,
            AssetVariant.filename.like("%.mp4"),
        )
    )
    broken = result.scalars().all()

    fixed_ids: list[uuid.UUID] = []
    for variant in broken:
        await db.refresh(variant, ["source_asset"])
        correct_ext = image_variant_ext(variant.source_asset.filename)
        stem = variant.filename.rsplit(".", 1)[0]
        variant.filename = f"{stem}{correct_ext}"
        variant.status = VariantStatus.PENDING
        variant.size_bytes = 0
        variant.checksum = ""
        variant.progress = 0.0
        variant.error_message = ""
        fixed_ids.append(variant.id)

    if broken:
        await db.commit()
        await enqueue_variants(db, fixed_ids)
        logger.info("Fixed %d image variant(s) with incorrect .mp4 extension", len(broken))

    return len(broken)


def get_transcode_status() -> dict:
    """Quick status for dashboard — queries are done in the caller."""
    return {}


async def _enqueue_transcoding_for_asset(
    asset: Asset, db: AsyncSession
) -> list[uuid.UUID]:
    """Create pending AssetVariant rows for all device profiles.

    Returns the list of newly-created variant ids so the caller can
    pass them to :func:`enqueue_variants`.
    """
    result = await db.execute(select(DeviceProfile))
    profiles = result.scalars().all()
    new_variant_ids: list[uuid.UUID] = []
    for profile in profiles:
        # Skip disabled profiles — no new variants until re-enabled.
        if not getattr(profile, "enabled", True):
            continue
        # Composed slides only get thumbnail-purpose variants (no source
        # file to ffmpeg under a device profile).
        if not _profile_emits_for_asset(profile, asset):
            continue
        existing = await db.execute(
            select(AssetVariant.id).where(
                AssetVariant.source_asset_id == asset.id,
                AssetVariant.profile_id == profile.id,
                AssetVariant.deleted_at.is_(None),
            ).limit(1)
        )
        if existing.scalar_one_or_none() is not None:
            continue

        variant_id = uuid.uuid4()
        ext = _variant_ext_for(asset, profile)
        variant = AssetVariant(
            id=variant_id,
            source_asset_id=asset.id,
            profile_id=profile.id,
            filename=f"{variant_id}{ext}",
        )
        db.add(variant)
        new_variant_ids.append(variant_id)

    if new_variant_ids:
        await db.commit()
    return new_variant_ids


async def recover_stalled_variants_once(db: AsyncSession) -> list[uuid.UUID]:
    """Re-enqueue variants whose worker has stopped heartbeating.

    This is a **liveness** check, not a duration budget.  The previous
    implementation measured age from ``AssetVariant.created_at`` — total
    elapsed wall-clock time — so any transcode legitimately slower than the
    timeout was declared stale and re-enqueued on every tick.  In production
    a single 72-minute 1080p transcode spawned three duplicate workers, all
    writing the same output blob concurrently.

    The signal is ``Job.heartbeat_at``, stamped by the worker every 15s.
    ``AssetVariant.progress`` is deliberately *not* used: it is only written
    when ffmpeg reports a duration (never for livestreams or un-probeable
    inputs), it stops entirely once the estimate clamps at 99%, and the
    image / thumbnail / webpage branches jump 0 → 100 with nothing in
    between.  Keying off progress would recreate the identical
    duplicate-worker bug for a different class of input.

    Variants with *no* active job are deliberately out of scope — that is
    :func:`reconcile_stranded_variants_once`'s responsibility.  This pass is
    only a backstop for a job that is still active while the worker holding
    it has died without marking it.

    Returns the ids of the variants that were recovered.
    """
    from datetime import datetime, timezone, timedelta

    from shared.models.job import Job, JobStatus

    cutoff = datetime.now(timezone.utc) - timedelta(
        seconds=_STALE_HEARTBEAT_TIMEOUT
    )
    result = await db.execute(
        select(Job, AssetVariant)
        .join(AssetVariant, AssetVariant.id == Job.target_id)
        .where(
            Job.type == JobType.VARIANT_TRANSCODE,
            Job.status == JobStatus.PROCESSING,
            AssetVariant.status == VariantStatus.PROCESSING,
            AssetVariant.deleted_at.is_(None),
            # COALESCE covers jobs claimed before this column existed, so a
            # deploy landing mid-transcode doesn't reap every in-flight job
            # on the first tick.
            func.coalesce(Job.heartbeat_at, Job.created_at) < cutoff,
        )
        .order_by(Job.created_at)
        .limit(_STALE_RESET_BATCH)
    )
    rows = result.all()
    if not rows:
        return []

    variant_ids = [v.id for _job, v in rows]

    # Ordering is load-bearing.  ``enqueue_jobs`` commits in its own
    # transaction, so creating the replacement *after* terminalising the old
    # job would leave a window in which the variant is non-terminal with no
    # active job — exactly the stranded-variant reconciler's candidate
    # shape.  That reconciler runs every 15s under a *different* advisory
    # lock in a different replica, so the window is genuinely reachable.
    # Enqueue first: an active job then exists at every instant.
    await enqueue_variants(db, variant_ids)

    for job, variant in rows:
        # FAILED, not CANCELLED: the reconciler terminalises a variant whose
        # newest job is CANCELLED unconditionally, but only acts on FAILED
        # once retries are exhausted.  retry_count is left untouched, so
        # this stays inert to it.
        job.status = JobStatus.FAILED
        job.error_message = (
            f"no heartbeat for >{_STALE_HEARTBEAT_TIMEOUT}s — worker presumed dead"
        )
        job.completed_at = datetime.now(timezone.utc)
        variant.status = VariantStatus.PENDING
        variant.progress = 0.0

    await db.commit()

    logger.warning(
        "Recovered %d variant(s) whose worker stopped heartbeating for >%ds: %s",
        len(rows), _STALE_HEARTBEAT_TIMEOUT,
        ", ".join(str(vid) for vid in variant_ids),
    )
    return variant_ids


async def stream_capture_monitor_loop() -> None:
    """Background loop: reconcile captures and reset stale work.

    Runs in the CMS process as an asyncio task (same pattern as
    ``scheduler_loop``).

    Two reconciliation phases per tick:

    1. **Completed captures → variants**.  A SAVED_STREAM with size_bytes>0
       and no variants means the worker just finished capturing.  Create
       the variant rows and enqueue VARIANT_TRANSCODE jobs.

    2. **Stale PROCESSING variants**.  A PROCESSING variant with no progress
       after the stale timeout had its worker crash.  Reset to PENDING and
       enqueue a fresh job.

    (Orphan-job sweeping used to live here; the transactional outbox
    — see ``shared.services.jobs.drain_outbox`` — now guarantees every
    committed Job has a queue message, so the sweep is no longer needed.)
    """
    from datetime import datetime, timezone, timedelta
    from cms.database import get_db

    logger.info(
        "Stream capture monitor started "
        "(interval=%ds, stale_heartbeat_timeout=%ds)",
        _MONITOR_INTERVAL, _STALE_HEARTBEAT_TIMEOUT,
    )

    while True:
        try:
            await asyncio.sleep(_MONITOR_INTERVAL)

            # Stage 4 (#344): gate the tick with a session-advisory lock
            # so only one replica runs the reconciliation pass.  Creating
            # variant rows is NOT idempotent across races (we'd end up
            # with duplicates pointing at the same capture), so this
            # needs an actual lock rather than a best-effort dedupe.
            from cms.services.leader import session_advisory_lock
            _MONITOR_LOCK_ID = 0x4147_4F52_41_03  # 'AGORA' + 03
            async with session_advisory_lock(_MONITOR_LOCK_ID) as got:
                if not got:
                    continue

                # ── 1. Completed captures → enqueue variants ──
                async for db in get_db():
                    result = await db.execute(
                        select(Asset).where(
                            Asset.asset_type == AssetType.SAVED_STREAM,
                            Asset.size_bytes > 0,
                            Asset.deleted_at.is_(None),
                            ~Asset.id.in_(
                                select(AssetVariant.source_asset_id).distinct()
                            ),
                        )
                    )
                    ready_assets = result.scalars().all()

                    for asset in ready_assets:
                        variant_ids = await _enqueue_transcoding_for_asset(asset, db)
                        if variant_ids:
                            await enqueue_variants(db, variant_ids)
                            logger.info(
                                "Stream capture complete for %s — enqueued %d variant(s)",
                                asset.id, len(variant_ids),
                            )

                # ── 2. PROCESSING variants whose worker stopped heartbeating ──
                async for db in get_db():
                    await recover_stalled_variants_once(db)

        except asyncio.CancelledError:
            logger.info("Stream capture monitor shutting down")
            raise
        except Exception:
            logger.exception("Error in stream capture monitor loop")


# ── Outbox drainer loop ─────────────────────────────────────────


async def outbox_drain_loop() -> None:
    """Background loop: drain the JobOutbox to the queue.

    Runs every ``_OUTBOX_DRAIN_INTERVAL`` seconds and calls
    :func:`shared.services.jobs.drain_outbox`, which sends pending queue
    messages and deletes the outbox rows on success.  In Postgres mode the
    poll is supplemented by ``LISTEN transcode_outbox`` (TODO: future
    enhancement; the 5s poll is the initial implementation).

    The drainer is idempotent and safe under concurrent CMS replicas:
    overlapping sends produce duplicate queue messages which the worker
    dedupes via ``claim_job``'s DONE-detection path.
    """
    from cms.database import get_db

    logger.info("Outbox drainer started (interval=%ds)", _OUTBOX_DRAIN_INTERVAL)
    while True:
        try:
            await asyncio.sleep(_OUTBOX_DRAIN_INTERVAL)
            async for db in get_db():
                try:
                    await drain_outbox(db)
                except Exception:
                    logger.exception("drain_outbox failed")
        except asyncio.CancelledError:
            logger.info("Outbox drainer shutting down")
            raise
        except Exception:
            logger.exception("Error in outbox drain loop")


# ── Soft-delete reaper ──────────────────────────────────────────

_REAPER_INTERVAL = int(os.environ.get("AGORA_REAPER_INTERVAL", "15"))


async def reap_deleted_assets_once(db, settings=None) -> int:
    """Run one pass of the reaper against the provided session.

    Returns the number of assets hard-deleted.  Exposed as a module-level
    helper so tests can drive the reaper deterministically without having
    to start the background loop.  ``settings`` may be passed to override
    the default ``cms.auth.get_settings()`` lookup (used by tests where the
    storage path lives under ``tmp_path``).
    """
    from cms.auth import get_settings as _get_settings
    from cms.models.asset import Asset as _Asset, AssetVariant as _AssetVariant, DeviceAsset as _DeviceAsset
    from cms.models.device import Device as _Device, DeviceGroup as _DeviceGroup
    from cms.models.group_asset import GroupAsset as _GroupAsset
    from shared.models.job import Job, JobType, JobStatus
    from cms.services.storage import get_storage
    from sqlalchemy import update, delete

    if settings is None:
        settings = _get_settings()
    storage = get_storage()
    active_statuses = [JobStatus.PENDING, JobStatus.PROCESSING]

    result = await db.execute(select(_Asset).where(_Asset.deleted_at.is_not(None)))
    pending_reap = result.scalars().all()

    reaped = 0
    for asset in pending_reap:
        variant_ids = (
            await db.execute(
                select(_AssetVariant.id).where(_AssetVariant.source_asset_id == asset.id)
            )
        ).scalars().all()

        active_cond = (
            (Job.type == JobType.STREAM_CAPTURE) & (Job.target_id == asset.id)
        )
        if variant_ids:
            active_cond = active_cond | (
                (Job.type == JobType.VARIANT_TRANSCODE) & (Job.target_id.in_(variant_ids))
            )
        active_count = await db.scalar(
            select(func.count()).select_from(Job).where(
                Job.status.in_(active_statuses),
                active_cond,
            )
        )
        if active_count:
            logger.info(
                "Reaper: asset %s (%s) has %d active job(s), skipping hard-delete",
                asset.id, asset.filename, active_count,
            )
            continue

        try:
            file_path = settings.asset_storage_path / asset.filename
            try:
                if file_path.is_file():
                    file_path.unlink()
            except Exception:
                logger.warning("Reaper: failed to unlink %s", file_path, exc_info=True)
            try:
                await storage.on_file_deleted(asset.filename)
            except Exception:
                logger.debug("Reaper: storage delete %s failed (likely already gone)", asset.filename)

            if asset.original_filename:
                orig_path = settings.asset_storage_path / "originals" / asset.original_filename
                try:
                    if orig_path.is_file():
                        orig_path.unlink()
                except Exception:
                    pass
                try:
                    await storage.on_file_deleted(f"originals/{asset.original_filename}")
                except Exception:
                    pass

            variants_dir = settings.asset_storage_path / "variants"
            var_result = await db.execute(
                select(_AssetVariant).where(_AssetVariant.source_asset_id == asset.id)
            )
            for variant in var_result.scalars().all():
                vpath = variants_dir / variant.filename
                try:
                    if vpath.is_file():
                        vpath.unlink()
                except Exception:
                    pass
                try:
                    await storage.on_file_deleted(f"variants/{variant.filename}")
                except Exception:
                    pass

            if variant_ids:
                await db.execute(
                    delete(Job).where(
                        (
                            (Job.type == JobType.VARIANT_TRANSCODE)
                            & (Job.target_id.in_(variant_ids))
                        ) | (
                            (Job.type == JobType.STREAM_CAPTURE)
                            & (Job.target_id == asset.id)
                        )
                    )
                )
            else:
                await db.execute(
                    delete(Job).where(
                        (Job.type == JobType.STREAM_CAPTURE)
                        & (Job.target_id == asset.id)
                    )
                )

            await db.execute(
                delete(_DeviceAsset).where(_DeviceAsset.asset_id == asset.id)
            )
            await db.execute(
                update(_Device).where(_Device.default_asset_id == asset.id).values(default_asset_id=None)
            )
            await db.execute(
                update(_DeviceGroup).where(_DeviceGroup.default_asset_id == asset.id).values(default_asset_id=None)
            )
            await db.execute(
                delete(_AssetVariant).where(_AssetVariant.source_asset_id == asset.id)
            )
            await db.execute(
                delete(_GroupAsset).where(_GroupAsset.asset_id == asset.id)
            )
            await db.delete(asset)
            await db.commit()
            reaped += 1

            logger.info("Reaper: hard-deleted asset %s (%s)", asset.id, asset.filename)
        except Exception:
            logger.exception("Reaper: failed to hard-delete asset %s", asset.id)
            try:
                await db.rollback()
            except Exception:
                pass

    return reaped


async def supersede_ready_variants_once(db) -> int:
    """Soft-delete older READY variants once a newer READY sibling exists.

    Part of the variant-swap flow: after a profile edit we insert a fresh
    PENDING variant row for each affected asset.  The OLD variant row is
    left intact so devices keep streaming the last good blob.  Once the
    NEW variant transitions to READY (worker sets status → complete), we
    must mark the older sibling(s) soft-deleted so the scheduler/resolver
    stop handing out stale checksums.

    A variant V is considered "superseded" when there exists another
    non-deleted AssetVariant V' with the same (source_asset_id,
    profile_id) where V'.status = READY and V'.created_at > V.created_at.
    Only V's that are themselves in a terminal state — READY, FAILED, or
    CANCELLED — are soft-deleted here; none of those can ever be promoted
    above a newer READY.  Still-PENDING or PROCESSING sibling jobs are
    left to run their course.

    Returns the number of variants soft-deleted this pass.
    """
    from cms.models.asset import AssetVariant as _AssetVariant, VariantStatus as _VariantStatus
    from datetime import datetime, timezone
    from sqlalchemy import and_
    from sqlalchemy.orm import aliased

    V = _AssetVariant
    V_newer = aliased(_AssetVariant)

    newer_exists = (
        select(V_newer.id).where(
            V_newer.source_asset_id == V.source_asset_id,
            V_newer.profile_id == V.profile_id,
            V_newer.status == _VariantStatus.READY,
            V_newer.deleted_at.is_(None),
            V_newer.created_at > V.created_at,
        )
    ).exists()

    result = await db.execute(
        select(V).where(
            V.deleted_at.is_(None),
            V.status.in_([
                _VariantStatus.READY,
                _VariantStatus.FAILED,
                _VariantStatus.CANCELLED,
            ]),
            newer_exists,
        )
    )
    to_mark = result.scalars().all()

    if not to_mark:
        return 0

    now = datetime.now(timezone.utc)
    for v in to_mark:
        v.deleted_at = now
        logger.info(
            "Reaper: soft-deleted superseded variant %s (asset=%s profile=%s "
            "status=%s filename=%s)",
            v.id, v.source_asset_id, v.profile_id, v.status.value, v.filename,
        )

    await db.commit()
    return len(to_mark)


async def reap_superseded_variants_once(db, settings=None) -> int:
    """Hard-delete soft-deleted variant rows whose jobs are terminal.

    Mirror of :func:`reap_deleted_assets_once` but scoped to individual
    AssetVariant rows marked ``deleted_at IS NOT NULL`` by the supersession
    sweep or other flows (e.g. a future "delete single variant" endpoint).

    A variant is eligible for hard-delete when no PENDING/PROCESSING
    ``VARIANT_TRANSCODE`` Job targets it.  We delete the blob then the
    row (plus any terminal Job rows that referenced it, to keep the jobs
    table tidy).

    Returns the number of variants hard-deleted this pass.
    """
    from cms.auth import get_settings as _get_settings
    from cms.models.asset import AssetVariant as _AssetVariant
    from shared.models.job import Job, JobType, JobStatus
    from cms.services.storage import get_storage
    from sqlalchemy import delete

    if settings is None:
        settings = _get_settings()
    storage = get_storage()
    active_statuses = [JobStatus.PENDING, JobStatus.PROCESSING]

    result = await db.execute(
        select(_AssetVariant).where(_AssetVariant.deleted_at.is_not(None))
    )
    pending = result.scalars().all()

    reaped = 0
    for variant in pending:
        active = await db.scalar(
            select(func.count()).select_from(Job).where(
                Job.status.in_(active_statuses),
                Job.type == JobType.VARIANT_TRANSCODE,
                Job.target_id == variant.id,
            )
        )
        if active:
            logger.info(
                "Reaper: variant %s (%s) has %d active job(s), "
                "skipping hard-delete",
                variant.id, variant.filename, active,
            )
            continue

        try:
            vpath = settings.asset_storage_path / "variants" / variant.filename
            try:
                if vpath.is_file():
                    vpath.unlink()
            except Exception:
                logger.warning(
                    "Reaper: failed to unlink variant blob %s",
                    vpath, exc_info=True,
                )
            try:
                await storage.on_file_deleted(f"variants/{variant.filename}")
            except Exception:
                logger.debug(
                    "Reaper: storage delete variants/%s failed (likely gone)",
                    variant.filename,
                )

            # Remove terminal Job rows referencing this variant so the jobs
            # table doesn't grow unbounded over many profile edits.
            await db.execute(
                delete(Job).where(
                    Job.type == JobType.VARIANT_TRANSCODE,
                    Job.target_id == variant.id,
                )
            )
            await db.delete(variant)
            await db.commit()
            reaped += 1

            logger.info(
                "Reaper: hard-deleted superseded variant %s (%s)",
                variant.id, variant.filename,
            )
        except Exception:
            logger.exception(
                "Reaper: failed to hard-delete variant %s", variant.id
            )
            try:
                await db.rollback()
            except Exception:
                pass

    return reaped


# ── Transcode-failure notifications ──────────────────────────────
#
# Title used to tag (and later find/prune) transcode-failure notifications.
# Kept as a module constant so the clear-errors endpoint can match on it.
TRANSCODE_FAIL_NOTIFICATION_TITLE = "Transcode failed"


async def reconcile_transcode_failure_notifications_once(db) -> int:
    """Push permanent transcode failures into the notification bell.

    Centralized, push-based reconciler: for every currently-FAILED (and not
    soft-deleted) transcode variant that doesn't already have a notification,
    create a ``scope="system"``, ``level="error"`` notification.  Conversely,
    prune notifications whose variant is no longer FAILED (recovered, deleted,
    or cleared via the clear-errors endpoint) so the bell self-heals.

    Running this in the reaper loop (single replica, under advisory lock) means
    we never double-emit, and it covers *every* path a variant can reach FAILED
    (worker ``_mark_failed``, the 2-hour-timeout bulk update, retry exhaustion)
    without hooking each site — and without the worker needing to import the
    CMS-only ``Notification`` model.

    Dedup key is ``details['variant_id']``.  Returns the number of
    notifications created this pass.
    """
    from cms.models.asset import AssetVariant, VariantStatus
    from cms.models.notification import Notification
    from cms.models.asset import Asset

    failed_rows = (await db.execute(
        select(
            AssetVariant.id,
            AssetVariant.source_asset_id,
            AssetVariant.error_message,
        ).where(
            AssetVariant.status == VariantStatus.FAILED,
            AssetVariant.deleted_at.is_(None),
        )
    )).all()
    failed_ids = {str(r[0]) for r in failed_rows}

    existing = (await db.execute(
        select(Notification).where(
            Notification.title == TRANSCODE_FAIL_NOTIFICATION_TITLE
        )
    )).scalars().all()

    notified_ids: set[str] = set()
    stale: list[Notification] = []
    for n in existing:
        vid = (n.details or {}).get("variant_id")
        if vid is None:
            continue
        if vid in failed_ids:
            notified_ids.add(vid)
        else:
            stale.append(n)

    for n in stale:
        await db.delete(n)

    to_create = [r for r in failed_rows if str(r[0]) not in notified_ids]
    created = 0
    if to_create:
        asset_ids = {r[1] for r in to_create}
        name_rows = (await db.execute(
            select(Asset.id, Asset.filename).where(Asset.id.in_(asset_ids))
        )).all()
        names = {a_id: fn for a_id, fn in name_rows}
        for vid_u, src_id, err in to_create:
            filename = names.get(src_id) or "asset"
            db.add(Notification(
                scope="system",
                level="error",
                title=TRANSCODE_FAIL_NOTIFICATION_TITLE,
                message=f"Transcoding failed for {filename}.",
                details={
                    "variant_id": str(vid_u),
                    "asset_id": str(src_id),
                    "filename": filename,
                    "error": (err or "")[:500],
                },
            ))
            created += 1

    if created or stale:
        await db.commit()
    return created


async def reconcile_stranded_variants_once(db, limit: int = 100) -> int:
    """Repair variants left non-terminal by a job that has finished.

    Variant status and job status are written by different processes, so
    they drift. Known producers of drift, all confirmed in the code:

    * the pre-transcode cancel path used to mark only the job (fixed at
      source in ``worker/__main__.py``, but rows stranded before that fix
      have terminal jobs and nothing else will ever revisit them);
    * poison/retry-exhaustion, where the mirroring helper no-opped on
      transcode jobs;
    * crash / lease-loss / SIGTERM-persistence-failure paths.

    Deliberately narrow, because the cheap version of this is unsafe:

    * **Only live rows.** Soft-deleted assets belong to
      :func:`reap_deleted_assets_once`, which gates on job status and
      hard-deletes regardless of variant status.
    * **"No active job", not "a terminal job exists".** A variant can have
      several jobs -- ``enqueue_variants`` always inserts a new row, and
      the stale-PROCESSING monitor re-enqueues by creating another one
      without terminating the old. An old FAILED job sitting alongside a
      live one must not terminalise the variant.
    * **The newest job decides.** Older terminal jobs are history.
    * **FAILED only when genuinely poison.** ``claim_job`` flips a job to
      FAILED only once ``retry_count`` exceeds the limit; a FAILED row
      under the limit can still be redelivered and reopened, so we leave
      it alone.
    * **DONE is never repaired to READY.** READY asserts a valid blob plus
      complete metadata (size, checksum, dimensions); a job reporting
      success is not evidence of either, and a bogus READY variant would
      be served to devices. Normal ordering commits the variant READY
      *before* the job is marked DONE, so this combination is corruption.
      It is counted and logged loudly instead.

    Updates are conditional on the variant still being non-terminal, so a
    worker writing concurrently wins rather than being clobbered. The
    reaper's advisory lock only serialises CMS replicas -- workers do not
    take it -- so correctness has to come from the predicate, not the lock.

    Returns the number of variants repaired (the DONE mismatches are
    reported, not repaired, and are not counted here).
    """
    from shared.models.asset import AssetVariant as _AssetVariant
    from cms.models.asset import VariantStatus as _VariantStatus
    from shared.models.job import Job, JobType, JobStatus, MAX_JOB_RETRIES
    from shared.metrics import (
        variant_reconcile_total,
        ATTR_REASON,
        REASON_RECONCILE_CANCELLED,
        REASON_RECONCILE_POISON_FAILED,
        REASON_RECONCILE_DONE_MISMATCH,
    )
    from sqlalchemy import update as _sa_update

    non_terminal = [_VariantStatus.PENDING, _VariantStatus.PROCESSING]

    active_job_exists = (
        select(Job.id).where(
            Job.type == JobType.VARIANT_TRANSCODE,
            Job.target_id == _AssetVariant.id,
            Job.status.in_([JobStatus.PENDING, JobStatus.PROCESSING]),
        )
    ).exists()

    candidates = (await db.execute(
        select(_AssetVariant)
        .join(Asset, Asset.id == _AssetVariant.source_asset_id)
        .where(
            _AssetVariant.deleted_at.is_(None),
            _AssetVariant.status.in_(non_terminal),
            Asset.deleted_at.is_(None),
            ~active_job_exists,
        )
        .order_by(_AssetVariant.created_at)
        .limit(limit)
    )).scalars().all()

    if not candidates:
        return 0

    repaired = 0
    for v in candidates:
        newest = (await db.execute(
            select(Job)
            .where(
                Job.type == JobType.VARIANT_TRANSCODE,
                Job.target_id == v.id,
            )
            .order_by(Job.created_at.desc(), Job.id.desc())
            .limit(1)
        )).scalar_one_or_none()

        # No job at all: the variant was created but not yet enqueued (the
        # profiles router commits variants before enqueueing). Leave it.
        if newest is None:
            continue

        if newest.status == JobStatus.CANCELLED:
            new_status = _VariantStatus.CANCELLED
            reason = REASON_RECONCILE_CANCELLED
            message = newest.error_message or "cancelled"
        elif (newest.status == JobStatus.FAILED
                and newest.retry_count > MAX_JOB_RETRIES):
            new_status = _VariantStatus.FAILED
            reason = REASON_RECONCILE_POISON_FAILED
            message = newest.error_message or "job exceeded retry limit"
        elif newest.status == JobStatus.DONE:
            # Reported, never repaired -- see the docstring.
            logger.error(
                "Reaper: variant %s is %s but its newest job %s is DONE "
                "(asset=%s profile=%s). NOT synthesising READY -- a "
                "successful dispatch is not evidence of a valid blob or "
                "complete metadata. Investigate.",
                v.id, v.status.value, newest.id, v.source_asset_id,
                v.profile_id,
            )
            variant_reconcile_total.add(
                1, {ATTR_REASON: REASON_RECONCILE_DONE_MISMATCH}
            )
            continue
        else:
            # FAILED under the retry limit, or some non-terminal state that
            # raced our candidate query. Redelivery still owns it.
            continue

        result = await db.execute(
            _sa_update(_AssetVariant)
            .where(
                _AssetVariant.id == v.id,
                _AssetVariant.status.in_(non_terminal),
            )
            .values(
                status=new_status,
                progress=0.0,
                error_message=str(message)[:2000],
            )
        )
        if not result.rowcount:
            # A worker wrote a terminal status between our read and write.
            continue

        repaired += 1
        variant_reconcile_total.add(1, {ATTR_REASON: reason})
        logger.warning(
            "Reaper: reconciled variant %s %s -> %s from job %s (%s). A "
            "steady rate here means a write path is stranding variants "
            "and should be fixed at source.",
            v.id, v.status.value, new_status.value, newest.id, reason,
        )

    if repaired:
        await db.commit()
    return repaired


async def deleted_asset_reaper_loop() -> None:
    """Background loop: hard-delete soft-deleted assets whose jobs are terminal.

    Runs every ``AGORA_REAPER_INTERVAL`` seconds in the CMS process.  The
    per-tick body is in :func:`reap_deleted_assets_once` so tests can drive
    it deterministically.

    Also runs per-variant supersession sweeps (see
    :func:`supersede_ready_variants_once` and
    :func:`reap_superseded_variants_once`) so profile-change variant swaps
    converge on a single READY variant per (asset, profile) pair.
    """
    from cms.database import get_db

    logger.info(
        "Deleted asset reaper started (interval=%ds, variant-supersession=ON)",
        _REAPER_INTERVAL,
    )

    while True:
        try:
            await asyncio.sleep(_REAPER_INTERVAL)
            # Stage 4 (#344): advisory-lock so only one replica runs the
            # blob-delete + supersession sweep at a time.  All three
            # operations are idempotent at the individual row level but
            # the external blob-delete API calls are wasted work if
            # duplicated across replicas.
            from cms.services.leader import session_advisory_lock
            _REAPER_LOCK_ID = 0x4147_4F52_41_04  # 'AGORA' + 04
            async with session_advisory_lock(_REAPER_LOCK_ID) as got:
                if not got:
                    continue
                async for db in get_db():
                    try:
                        await reap_deleted_assets_once(db)
                    except Exception:
                        logger.exception("Reaper: asset sweep failed")
                async for db in get_db():
                    try:
                        # Before the supersession sweeps: they can only act
                        # on variants that are READY/FAILED/CANCELLED, so a
                        # variant stranded non-terminal is invisible to them
                        # until this runs first.
                        fixed = await reconcile_stranded_variants_once(db)
                        if fixed:
                            logger.warning(
                                "Reaper: reconciled %d variant(s) left "
                                "non-terminal by a finished job", fixed,
                            )
                    except Exception:
                        logger.exception("Reaper: variant reconciliation sweep failed")
                async for db in get_db():
                    try:
                        marked = await supersede_ready_variants_once(db)
                        if marked:
                            logger.info(
                                "Reaper: supersession sweep soft-deleted %d "
                                "stale variant(s)", marked,
                            )
                    except Exception:
                        logger.exception("Reaper: variant supersession sweep failed")
                async for db in get_db():
                    try:
                        reaped = await reap_superseded_variants_once(db)
                        if reaped:
                            logger.info(
                                "Reaper: hard-deleted %d superseded variant(s)",
                                reaped,
                            )
                    except Exception:
                        logger.exception("Reaper: variant hard-delete sweep failed")
                async for db in get_db():
                    try:
                        emitted = await reconcile_transcode_failure_notifications_once(db)
                        if emitted:
                            logger.info(
                                "Reaper: emitted %d transcode-failure notification(s)",
                                emitted,
                            )
                    except Exception:
                        logger.exception(
                            "Reaper: transcode-failure notification sweep failed"
                        )
        except asyncio.CancelledError:
            logger.info("Deleted asset reaper shutting down")
            raise
        except Exception:
            logger.exception("Error in deleted asset reaper loop")
