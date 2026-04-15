"""Transcoder service — CMS-side shim.

Transcoding has moved to the dedicated worker container (worker/).
This module retains:
  - DB-only helpers (enqueue, fix extensions) called from CMS routers
  - probe_media re-export from shared
  - convert_image re-export from shared (HEIC→JPEG on upload)
  - No-op cancel stubs (worker handles its own cancellation)
"""

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.asset import Asset, AssetType, AssetVariant, VariantStatus
from shared.models.device_profile import DeviceProfile
from shared.services.image import convert_image, convert_image_to_jpeg, image_variant_ext  # noqa: F401
from shared.services.probe import probe_media  # noqa: F401

logger = logging.getLogger("agora.cms.transcoder")


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
    try:
        await db.execute(text("NOTIFY transcode_jobs"))
        await db.commit()
    except Exception:
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
        logger.debug("Azure queue enqueue failed (non-critical)", exc_info=True)
