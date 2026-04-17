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
from shared.services.jobs import enqueue_job, enqueue_jobs, sweep_orphans
from shared.services.probe import probe_media  # noqa: F401

logger = logging.getLogger("agora.cms.transcoder")

# ── Monitor loop intervals ──────────────────────────────────────
_MONITOR_INTERVAL = int(os.environ.get("AGORA_MONITOR_INTERVAL", "30"))
_STALE_PROCESSING_TIMEOUT = int(os.environ.get("AGORA_STALE_TIMEOUT", "1800"))  # 30 min
_ORPHAN_JOB_AGE_SECONDS = int(os.environ.get("AGORA_ORPHAN_JOB_AGE", "120"))    # 2 min


def _image_variant_ext(asset) -> str:
    """Return the correct file extension for an image variant."""
    return image_variant_ext(asset.filename)


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
    return result.rowcount or 0


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
    """Create pending variants for all video + image assets for a new profile.

    Returns the list of newly-created variant ids (caller may pass these to
    :func:`enqueue_variants`).
    """
    result = await db.execute(
        select(Asset).where(
            Asset.asset_type.in_([AssetType.VIDEO, AssetType.IMAGE]),
            Asset.deleted_at.is_(None),
        )
    )
    assets = result.scalars().all()

    profile_result = await db.execute(
        select(DeviceProfile).where(DeviceProfile.id == profile_id)
    )
    profile = profile_result.scalar_one_or_none()
    if not profile:
        return []

    new_variant_ids: list[uuid.UUID] = []
    for asset in assets:
        existing = await db.execute(
            select(AssetVariant).where(
                AssetVariant.source_asset_id == asset.id,
                AssetVariant.profile_id == profile_id,
            )
        )
        if existing.scalar_one_or_none():
            continue

        variant_id = uuid.uuid4()
        if asset.asset_type == AssetType.IMAGE:
            ext = image_variant_ext(asset.filename)
        elif profile.audio_codec == "libopus":
            ext = ".mkv"
        else:
            ext = ".mp4"
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
        existing = await db.execute(
            select(AssetVariant.id).where(
                AssetVariant.source_asset_id == asset.id,
                AssetVariant.profile_id == profile.id,
            ).limit(1)
        )
        if existing.scalar_one_or_none() is not None:
            continue

        variant_id = uuid.uuid4()
        if asset.asset_type == AssetType.IMAGE:
            ext = image_variant_ext(asset.filename)
        elif profile.audio_codec == "libopus":
            ext = ".mkv"
        else:
            ext = ".mp4"
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


async def stream_capture_monitor_loop() -> None:
    """Background loop: reconcile captures, sweep orphans, reset stale work.

    Runs in the CMS process as an asyncio task (same pattern as
    ``scheduler_loop``).

    Three reconciliation phases per tick:

    1. **Completed captures → variants**.  A SAVED_STREAM with size_bytes>0
       and no variants means the worker just finished capturing.  Create
       the variant rows and enqueue VARIANT_TRANSCODE jobs.

    2. **Orphan jobs**.  Any PENDING job older than ``_ORPHAN_JOB_AGE_SECONDS``
       likely lost its queue message (CMS crashed between commit and send,
       or transient queue error).  Re-send the queue message.

    3. **Stale PROCESSING variants**.  A PROCESSING variant with no progress
       after the stale timeout had its worker crash.  Reset to PENDING and
       enqueue a fresh job.
    """
    from datetime import datetime, timezone, timedelta
    from cms.database import get_db

    logger.info(
        "Stream capture monitor started "
        "(interval=%ds, stale_timeout=%ds, orphan_age=%ds)",
        _MONITOR_INTERVAL, _STALE_PROCESSING_TIMEOUT, _ORPHAN_JOB_AGE_SECONDS,
    )

    while True:
        try:
            await asyncio.sleep(_MONITOR_INTERVAL)

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

            # ── 2. Orphan job sweep ──
            async for db in get_db():
                try:
                    await sweep_orphans(db, stale_seconds=_ORPHAN_JOB_AGE_SECONDS)
                except Exception:
                    logger.exception("Orphan job sweep failed")

            # ── 3. Stale PROCESSING variants → reset to PENDING ──
            async for db in get_db():
                cutoff = datetime.now(timezone.utc) - timedelta(seconds=_STALE_PROCESSING_TIMEOUT)
                result = await db.execute(
                    select(AssetVariant).where(
                        AssetVariant.status == VariantStatus.PROCESSING,
                    )
                )
                processing = result.scalars().all()

                reset_ids: list[uuid.UUID] = []
                for v in processing:
                    age = (datetime.now(timezone.utc) - v.created_at).total_seconds()
                    if v.progress == 0.0 and age > _STALE_PROCESSING_TIMEOUT:
                        v.status = VariantStatus.PENDING
                        v.progress = 0.0
                        reset_ids.append(v.id)
                    elif age > _STALE_PROCESSING_TIMEOUT * 2:
                        v.status = VariantStatus.PENDING
                        v.progress = 0.0
                        reset_ids.append(v.id)

                if reset_ids:
                    await db.commit()
                    await enqueue_variants(db, reset_ids)
                    logger.warning(
                        "Reset %d stale PROCESSING variant(s) to PENDING", len(reset_ids),
                    )

        except asyncio.CancelledError:
            logger.info("Stream capture monitor shutting down")
            raise
        except Exception:
            logger.exception("Error in stream capture monitor loop")


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


async def deleted_asset_reaper_loop() -> None:
    """Background loop: hard-delete soft-deleted assets whose jobs are terminal.

    Runs every ``AGORA_REAPER_INTERVAL`` seconds in the CMS process.  The
    per-tick body is in :func:`reap_deleted_assets_once` so tests can drive
    it deterministically.
    """
    from cms.database import get_db

    logger.info("Deleted asset reaper started (interval=%ds)", _REAPER_INTERVAL)

    while True:
        try:
            await asyncio.sleep(_REAPER_INTERVAL)
            async for db in get_db():
                await reap_deleted_assets_once(db)
        except asyncio.CancelledError:
            logger.info("Deleted asset reaper shutting down")
            raise
        except Exception:
            logger.exception("Error in deleted asset reaper loop")
