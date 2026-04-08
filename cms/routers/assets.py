"""Asset library API routes."""

import hashlib
import re
import uuid
from pathlib import Path
from typing import List

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from cms.auth import get_settings, require_auth
from cms.config import Settings
from cms.database import get_db
from cms.models.asset import Asset, AssetType, AssetVariant, DeviceAsset, VariantStatus
from cms.models.device import Device, DeviceGroup
from cms.models.device_profile import DeviceProfile
from cms.models.schedule import Schedule
from cms.schemas.asset import AssetOut

router = APIRouter(prefix="/api/assets", dependencies=[Depends(require_auth)])

# Separate router for device-facing endpoints (no admin auth required)
device_router = APIRouter(prefix="/api/assets")

ALLOWED_PATTERN = re.compile(
    r"^[a-zA-Z0-9_\-][a-zA-Z0-9_\-. ]{0,200}"
    r"\.(mp4|mov|mkv|avi|webm|ts|m4v|jpg|jpeg|png|heif|heic|avif|webp|gif|bmp|tiff|tif)$",
    re.IGNORECASE,
)
MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB for source videos

# Formats that need conversion to JPEG for device compatibility
IMAGE_CONVERT_EXTS = {".heif", ".heic", ".avif", ".webp", ".bmp", ".tiff", ".tif", ".gif"}


def _asset_type(filename: str) -> AssetType:
    ext = filename.rsplit(".", 1)[-1].lower()
    if ext in ("mp4", "mov", "mkv", "avi", "webm", "ts", "m4v"):
        return AssetType.VIDEO
    return AssetType.IMAGE


@router.get("/status")
async def assets_status_json(db: AsyncSession = Depends(get_db)):
    """Lightweight JSON for assets page polling.

    Returns global variant counts for structural-change detection, plus
    per-asset variant summaries so the UI can update badges and expanded
    detail panels in-place without a full page reload.
    """
    from sqlalchemy import func as sa_func
    from sqlalchemy.orm import selectinload

    asset_count = (await db.execute(select(sa_func.count(Asset.id)))).scalar() or 0
    variant_ready = (await db.execute(
        select(sa_func.count()).select_from(AssetVariant).where(AssetVariant.status == VariantStatus.READY)
    )).scalar() or 0
    variant_processing = (await db.execute(
        select(sa_func.count()).select_from(AssetVariant).where(AssetVariant.status == VariantStatus.PROCESSING)
    )).scalar() or 0
    variant_failed = (await db.execute(
        select(sa_func.count()).select_from(AssetVariant).where(AssetVariant.status == VariantStatus.FAILED)
    )).scalar() or 0

    # Per-asset variant details for in-place UI updates
    result = await db.execute(
        select(Asset)
        .options(selectinload(Asset.variants).selectinload(AssetVariant.profile))
        .order_by(Asset.uploaded_at.desc())
    )
    assets_detail = []
    for a in result.scalars().all():
        variants = []
        a_ready = a_processing = a_failed = 0
        for v in a.variants:
            vd = {
                "id": str(v.id),
                "profile_name": v.profile.name if v.profile else "",
                "status": v.status.value,
                "progress": v.progress,
                "error_message": v.error_message or "",
                "width": v.width,
                "height": v.height,
                "video_codec": v.video_codec,
                "bitrate": v.bitrate,
                "frame_rate": v.frame_rate,
                "size_bytes": v.size_bytes,
                "checksum": v.checksum or "",
            }
            variants.append(vd)
            if v.status == VariantStatus.READY:
                a_ready += 1
            elif v.status == VariantStatus.PROCESSING:
                a_processing += 1
            elif v.status == VariantStatus.FAILED:
                a_failed += 1
        assets_detail.append({
            "id": str(a.id),
            "variant_total": len(a.variants),
            "variant_ready": a_ready,
            "variant_processing": a_processing,
            "variant_failed": a_failed,
            "variants": variants,
        })

    return {
        "asset_count": asset_count,
        "variant_ready": variant_ready,
        "variant_processing": variant_processing,
        "variant_failed": variant_failed,
        "assets": assets_detail,
    }


@router.get("", response_model=List[AssetOut])
async def list_assets(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Asset).order_by(Asset.uploaded_at.desc()))
    return result.scalars().all()


@router.post("/upload", response_model=AssetOut, status_code=201)
async def upload_asset(
    file: UploadFile,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    if not file.filename or not ALLOWED_PATTERN.match(file.filename):
        raise HTTPException(status_code=400, detail="Invalid filename")

    # Check for duplicate
    existing = await db.execute(select(Asset).where(Asset.filename == file.filename))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Asset already exists")

    # Read and hash
    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File too large")
    checksum = hashlib.sha256(content).hexdigest()

    # Store source file
    storage_dir = settings.asset_storage_path
    storage_dir.mkdir(parents=True, exist_ok=True)
    dest = storage_dir / file.filename
    dest.write_bytes(content)

    asset_type = _asset_type(file.filename)

    # Convert unsupported image formats to JPEG for device compatibility
    ext = "." + file.filename.rsplit(".", 1)[-1].lower()
    final_filename = file.filename
    original_filename = None
    if asset_type == AssetType.IMAGE and ext in IMAGE_CONVERT_EXTS:
        from cms.services.transcoder import convert_image_to_jpeg
        jpeg_filename = Path(file.filename).stem + ".jpg"
        # Check the JPEG name doesn't conflict
        dup = await db.execute(select(Asset).where(Asset.filename == jpeg_filename))
        if dup.scalar_one_or_none():
            raise HTTPException(
                status_code=409,
                detail=f"Converted name '{jpeg_filename}' already exists",
            )
        jpeg_path = storage_dir / jpeg_filename
        ok = await convert_image_to_jpeg(dest, jpeg_path)
        if not ok:
            dest.unlink(missing_ok=True)
            raise HTTPException(status_code=422, detail="Image conversion failed")
        # Keep original in originals/ for future re-transcoding
        originals_dir = storage_dir / "originals"
        originals_dir.mkdir(parents=True, exist_ok=True)
        dest.rename(originals_dir / file.filename)
        original_filename = file.filename
        content = jpeg_path.read_bytes()
        checksum = hashlib.sha256(content).hexdigest()
        final_filename = jpeg_filename

    # Database record
    asset = Asset(
        filename=final_filename,
        original_filename=original_filename,
        asset_type=asset_type,
        size_bytes=len(content),
        checksum=checksum,
    )
    db.add(asset)
    await db.commit()
    await db.refresh(asset)

    # Probe media metadata in background
    from cms.services.transcoder import probe_media
    stored_path = settings.asset_storage_path / final_filename
    meta = await probe_media(stored_path)
    for key, val in meta.items():
        if val is not None:
            setattr(asset, key, val)
    await db.commit()

    # Queue transcoding for all profiles (video and image assets)
    if asset_type in (AssetType.VIDEO, AssetType.IMAGE):
        await _enqueue_transcoding(asset, db)

    return asset


async def _enqueue_transcoding(asset: Asset, db: AsyncSession) -> None:
    """Create pending AssetVariant rows for all device profiles."""
    result = await db.execute(select(DeviceProfile))
    profiles = result.scalars().all()
    for profile in profiles:
        variant_id = uuid.uuid4()
        if asset.asset_type == AssetType.IMAGE:
            from cms.services.transcoder import _image_variant_ext
            ext = _image_variant_ext(asset)
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
    await db.commit()


@router.get("/{asset_id}", response_model=AssetOut)
async def get_asset(asset_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Asset).where(Asset.id == asset_id))
    asset = result.scalar_one_or_none()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")
    return asset


@device_router.get("/{asset_id}/download")
async def download_asset(
    asset_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    result = await db.execute(select(Asset).where(Asset.id == asset_id))
    asset = result.scalar_one_or_none()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")

    file_path = settings.asset_storage_path / asset.filename
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found on disk")

    return FileResponse(
        path=file_path,
        filename=asset.filename,
        media_type="application/octet-stream",
    )


MIME_TYPES = {
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".mkv": "video/x-matroska",
    ".avi": "video/x-msvideo",
    ".webm": "video/webm",
    ".ts": "video/mp2t",
    ".m4v": "video/x-m4v",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


@router.get("/{asset_id}/preview")
async def preview_asset(
    asset_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    result = await db.execute(select(Asset).where(Asset.id == asset_id))
    asset = result.scalar_one_or_none()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")

    file_path = settings.asset_storage_path / asset.filename
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found on disk")

    ext = "." + asset.filename.rsplit(".", 1)[-1].lower()
    media_type = MIME_TYPES.get(ext, "application/octet-stream")

    return FileResponse(path=file_path, media_type=media_type)


@router.delete("/{asset_id}")
async def delete_asset(
    asset_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    result = await db.execute(select(Asset).where(Asset.id == asset_id))
    asset = result.scalar_one_or_none()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")

    # Block deletion if any schedule references this asset
    sched_count = await db.scalar(
        select(func.count()).select_from(Schedule).where(Schedule.asset_id == asset_id)
    )
    if sched_count:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot delete — asset is used by {sched_count} schedule(s). Remove it from all schedules first.",
        )

    # Remove source file
    file_path = settings.asset_storage_path / asset.filename
    if file_path.is_file():
        file_path.unlink()

    # Remove original file (if converted from HEIC/AVIF/etc)
    if asset.original_filename:
        orig_path = settings.asset_storage_path / "originals" / asset.original_filename
        if orig_path.is_file():
            orig_path.unlink()

    # Remove device-asset tracking records
    da_result = await db.execute(
        select(DeviceAsset).where(DeviceAsset.asset_id == asset_id)
    )
    for da in da_result.scalars().all():
        await db.delete(da)

    # Clear default_asset_id on devices/groups referencing this asset
    await db.execute(
        update(Device).where(Device.default_asset_id == asset_id).values(default_asset_id=None)
    )
    await db.execute(
        update(DeviceGroup).where(DeviceGroup.default_asset_id == asset_id).values(default_asset_id=None)
    )

    # Cancel any active transcode for this asset
    from cms.services.transcoder import cancel_asset_transcodes
    cancel_asset_transcodes(asset_id)

    # Remove variant files
    variants_dir = settings.asset_storage_path / "variants"
    var_result = await db.execute(
        select(AssetVariant).where(AssetVariant.source_asset_id == asset_id)
    )
    for variant in var_result.scalars().all():
        vpath = variants_dir / variant.filename
        if vpath.is_file():
            vpath.unlink()
        await db.delete(variant)

    await db.delete(asset)
    await db.commit()
    return {"deleted": asset.filename}


@device_router.get("/variants/{variant_id}/download")
async def download_variant(
    variant_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """Download a transcoded asset variant (used by devices)."""
    result = await db.execute(
        select(AssetVariant).where(
            AssetVariant.id == variant_id,
            AssetVariant.status == VariantStatus.READY,
        )
    )
    variant = result.scalar_one_or_none()
    if not variant:
        raise HTTPException(status_code=404, detail="Variant not found or not ready")

    file_path = settings.asset_storage_path / "variants" / variant.filename
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="Variant file not found on disk")

    # Serve with a human-readable download name
    await db.refresh(variant, ["source_asset", "profile"])
    variant_ext = Path(variant.filename).suffix
    download_name = f"{Path(variant.source_asset.filename).stem}_{variant.profile.name}{variant_ext}"

    return FileResponse(
        path=file_path,
        filename=download_name,
        media_type="application/octet-stream",
    )
