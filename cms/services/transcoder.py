"""Transcoder service — CMS-side shim.

Transcoding has moved to the dedicated worker container (worker/).
This module retains:
  - DB-only helpers (enqueue, fix extensions) called from CMS routers
  - stream_capture_monitor_loop — orchestrates stream capture → variant workflow
  - probe_media re-export from shared
  - convert_image re-export from shared (HEIC→JPEG on upload)
  - No-op cancel stubs (worker handles its own cancellation)
"""

import asyncio
import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.asset import Asset, AssetType, AssetVariant, VariantStatus
from shared.models.device_profile import DeviceProfile
from shared.services.image import convert_image, convert_image_to_jpeg, image_variant_ext  # noqa: F401
from shared.services.probe import probe_media  # noqa: F401

logger = logging.getLogger("agora.cms.transcoder")

# ── Stream capture monitor ──────────────────────────────────────
# How often to check for completed captures / stale processing (seconds)
_MONITOR_INTERVAL = int(__import__("os").environ.get("AGORA_MONITOR_INTERVAL", "30"))

# A PROCESSING variant with no progress update for this long is stale (seconds)
_STALE_PROCESSING_TIMEOUT = int(__import__("os").environ.get("AGORA_STALE_TIMEOUT", "1800"))  # 30 min


def _image_variant_ext(asset) -> str:
    """Return the correct file extension for an image variant.

    Wrapper around shared image_variant_ext for backward compatibility
    (existing callers pass an Asset object).
    """
    return image_variant_ext(asset.filename)


def cancel_profile_transcodes(profile_id: uuid.UUID) -> bool:
    """No-op — transcoding runs in the worker container."""
    return False


def cancel_asset_transcodes(asset_id: uuid.UUID) -> bool:
    """No-op — transcoding runs in the worker container."""
    return False


async def enqueue_for_new_profile(profile_id, db: AsyncSession) -> int:
    """Create pending variants for all video and image assets for a new profile.

    Returns the number of variants enqueued.
    """
    result = await db.execute(
        select(Asset).where(
            Asset.asset_type.in_([AssetType.VIDEO, AssetType.IMAGE])
        )
    )
    assets = result.scalars().all()

    profile_result = await db.execute(
        select(DeviceProfile).where(DeviceProfile.id == profile_id)
    )
    profile = profile_result.scalar_one_or_none()
    if not profile:
        return 0

    count = 0
    for asset in assets:
        # Check if variant already exists
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
        count += 1

    await db.commit()
    return count


async def fix_image_variant_extensions(db: AsyncSession) -> int:
    """Fix image variants that have incorrect .mp4 extensions.

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

    if broken:
        await db.commit()
        logger.info("Fixed %d image variant(s) with incorrect .mp4 extension", len(broken))

    return len(broken)


def get_transcode_status() -> dict:
    """Quick status for dashboard — queries are done in the caller."""
    return {}


async def notify_worker(db) -> None:
    """Wake the transcode worker.

    Docker Compose: sends a PostgreSQL NOTIFY on the transcode_jobs channel.
    Azure:          also enqueues a message on the Azure Storage Queue so
                    the Container Apps Job scales up.
    """
    from sqlalchemy import text
    # Only attempt NOTIFY on PostgreSQL — it is a PostgreSQL-specific command.
    # On SQLite (tests) this would fail and the rollback would expire all ORM
    # objects in the session, causing MissingGreenlet in callers.
    dialect = db.bind.dialect.name if db.bind else ""
    if dialect == "postgresql":
        try:
            await db.execute(text("NOTIFY transcode_jobs"))
            await db.commit()
        except Exception:
            await db.rollback()
            logger.debug("NOTIFY transcode_jobs failed (non-critical)", exc_info=True)

    # Azure Storage Queue trigger (Container Apps Job)
    try:
        import os
        conn_str = os.environ.get("AGORA_CMS_AZURE_STORAGE_CONNECTION_STRING")
        if conn_str:
            from azure.storage.queue import QueueClient
            queue = QueueClient.from_connection_string(conn_str, "transcode-jobs")
            queue.send_message("transcode")
            logger.debug("Enqueued transcode-jobs queue message")
    except Exception:
        logger.warning("Azure queue enqueue failed", exc_info=True)


async def _enqueue_transcoding_for_asset(asset: Asset, db: AsyncSession) -> int:
    """Create pending AssetVariant rows for all device profiles.

    This is the shared logic used by both the upload path (via the router)
    and the stream capture monitor.  Returns the number of variants created.
    """
    result = await db.execute(select(DeviceProfile))
    profiles = result.scalars().all()
    count = 0
    for profile in profiles:
        # Skip if variant already exists (idempotent)
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
        count += 1

    if count:
        await db.commit()
    return count


async def stream_capture_monitor_loop() -> None:
    """Background loop that orchestrates the stream capture → variant workflow.

    Periodically checks for:
    1. Completed stream captures (SAVED_STREAM with source file, no variants)
       → creates variant rows and notifies workers (identical to upload flow)
    2. Stale PROCESSING variants (no progress update beyond timeout)
       → resets to PENDING and re-notifies workers

    Runs in the CMS process as an asyncio task (same pattern as scheduler_loop).
    """
    from datetime import datetime, timezone, timedelta
    from cms.database import get_db

    logger.info(
        "Stream capture monitor started (interval=%ds, stale_timeout=%ds)",
        _MONITOR_INTERVAL, _STALE_PROCESSING_TIMEOUT,
    )

    while True:
        try:
            await asyncio.sleep(_MONITOR_INTERVAL)

            # ── 1. Completed captures → enqueue variants ──
            async for db in get_db():
                # Find SAVED_STREAM assets that have been captured (size_bytes > 0)
                # but don't yet have any variants — the CMS needs to create them.
                result = await db.execute(
                    select(Asset).where(
                        Asset.asset_type == AssetType.SAVED_STREAM,
                        Asset.size_bytes > 0,  # capture completed
                        ~Asset.id.in_(
                            select(AssetVariant.source_asset_id).distinct()
                        ),
                    )
                )
                ready_assets = result.scalars().all()

                for asset in ready_assets:
                    count = await _enqueue_transcoding_for_asset(asset, db)
                    if count:
                        await notify_worker(db)
                        logger.info(
                            "Stream capture complete for %s — enqueued %d variant(s)",
                            asset.id, count,
                        )

            # ── 2. Stale PROCESSING variants → reset to PENDING ──
            async for db in get_db():
                cutoff = datetime.now(timezone.utc) - timedelta(seconds=_STALE_PROCESSING_TIMEOUT)
                result = await db.execute(
                    select(AssetVariant).where(
                        AssetVariant.status == VariantStatus.PROCESSING,
                        # Use created_at as a conservative bound — if progress was
                        # updating, the row's updated_at would be recent.  We check
                        # progress == 0 as a stronger signal (never started).
                    )
                )
                processing = result.scalars().all()

                reset_count = 0
                for v in processing:
                    # Consider stale if created long ago AND still at 0% progress,
                    # OR if it's been processing for 2x the stale timeout (stuck at
                    # any progress level).
                    age = (datetime.now(timezone.utc) - v.created_at).total_seconds()
                    if v.progress == 0.0 and age > _STALE_PROCESSING_TIMEOUT:
                        v.status = VariantStatus.PENDING
                        v.progress = 0.0
                        reset_count += 1
                    elif age > _STALE_PROCESSING_TIMEOUT * 2:
                        v.status = VariantStatus.PENDING
                        v.progress = 0.0
                        reset_count += 1

                if reset_count:
                    await db.commit()
                    await notify_worker(db)
                    logger.warning(
                        "Reset %d stale PROCESSING variant(s) to PENDING", reset_count,
                    )

        except asyncio.CancelledError:
            logger.info("Stream capture monitor shutting down")
            raise
        except Exception:
            logger.exception("Error in stream capture monitor loop")
