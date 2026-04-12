"""Asset library API routes with RBAC group scoping."""

import hashlib
import re
import uuid
from pathlib import Path
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from cms.auth import get_settings, get_user_group_ids, require_auth, require_permission
from cms.config import Settings
from cms.database import get_db
from cms.permissions import ASSETS_READ, ASSETS_WRITE
from cms.models.asset import Asset, AssetType, AssetVariant, DeviceAsset, VariantStatus
from cms.models.device import Device, DeviceGroup
from cms.models.device_profile import DeviceProfile
from cms.models.group_asset import GroupAsset
from cms.models.schedule import Schedule
from cms.models.user import User
from cms.schemas.asset import AssetOut
from cms.services.storage import get_storage

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


async def _visible_asset_ids(user: User, db: AsyncSession) -> list[uuid.UUID] | None:
    """Return asset IDs visible to the user, or None if admin (see all).

    An asset is visible if:
    - it is global (is_global=True), OR
    - the user's groups include the asset's owner group, OR
    - the asset is shared with one of the user's groups via GroupAsset
    """
    group_ids = await get_user_group_ids(user, db)
    if group_ids is None:
        return None  # Admin — no filtering

    # Global assets
    global_q = select(Asset.id).where(Asset.is_global.is_(True))

    # Assets in the user's groups (via GroupAsset junction)
    group_q = (
        select(GroupAsset.asset_id)
        .where(GroupAsset.group_id.in_(group_ids))
    ) if group_ids else select(GroupAsset.asset_id).where(False)

    global_ids = set((await db.execute(global_q)).scalars().all())
    group_asset_ids = set((await db.execute(group_q)).scalars().all())

    return list(global_ids | group_asset_ids)


@router.get("/status", dependencies=[Depends(require_permission(ASSETS_READ))])
async def assets_status_json(
    user: User = Depends(require_permission(ASSETS_READ)),
    db: AsyncSession = Depends(get_db),
):
    """Lightweight JSON for assets page polling — filtered by user's group access."""
    from sqlalchemy import func as sa_func

    visible = await _visible_asset_ids(user, db)

    # Base query for assets this user can see
    asset_q = select(Asset)
    if visible is not None:
        asset_q = asset_q.where(Asset.id.in_(visible))

    asset_count = (await db.execute(
        select(sa_func.count(Asset.id)).where(Asset.id.in_(visible)) if visible is not None
        else select(sa_func.count(Asset.id))
    )).scalar() or 0

    variant_base = select(sa_func.count()).select_from(AssetVariant)
    if visible is not None:
        variant_base = variant_base.where(AssetVariant.source_asset_id.in_(visible))
    variant_ready = (await db.execute(
        variant_base.where(AssetVariant.status == VariantStatus.READY)
    )).scalar() or 0
    variant_processing = (await db.execute(
        variant_base.where(AssetVariant.status == VariantStatus.PROCESSING)
    )).scalar() or 0
    variant_failed = (await db.execute(
        variant_base.where(AssetVariant.status == VariantStatus.FAILED)
    )).scalar() or 0

    result = await db.execute(
        asset_q
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
async def list_assets(
    user: User = Depends(require_permission(ASSETS_READ)),
    db: AsyncSession = Depends(get_db),
):
    """List assets visible to the current user (filtered by group membership)."""
    visible = await _visible_asset_ids(user, db)
    q = select(Asset).order_by(Asset.uploaded_at.desc())
    if visible is not None:
        q = q.where(Asset.id.in_(visible))
    result = await db.execute(q)
    return result.scalars().all()


@router.post("/upload", response_model=AssetOut, status_code=201)
async def upload_asset(
    file: UploadFile,
    user: User = Depends(require_permission(ASSETS_WRITE)),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    group_id: str | None = Query(None, description="Owner group UUID"),
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
    storage = get_storage()
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
        # Sync converted JPEG + original to cloud storage
        await storage.on_file_stored(final_filename)
        await storage.on_file_stored(f"originals/{file.filename}")
    else:
        # Sync source file to cloud storage
        await storage.on_file_stored(file.filename)

    # Resolve owner group
    owner_uuid = None
    if group_id:
        try:
            owner_uuid = uuid.UUID(group_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid group_id")
        # Verify user has access to this group (or is admin)
        user_groups = await get_user_group_ids(user, db)
        if user_groups is not None and owner_uuid not in user_groups:
            raise HTTPException(status_code=403, detail="You are not a member of this group")

    # Database record
    asset = Asset(
        filename=final_filename,
        original_filename=original_filename,
        asset_type=asset_type,
        size_bytes=len(content),
        checksum=checksum,
        owner_group_id=owner_uuid,
        is_global=owner_uuid is None,  # No group → global
    )
    db.add(asset)
    await db.flush()

    # Create GroupAsset ownership entry
    if owner_uuid:
        db.add(GroupAsset(
            asset_id=asset.id,
            group_id=owner_uuid,
            is_owner=True,
        ))

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


@router.get("/{asset_id}", response_model=AssetOut, dependencies=[Depends(require_permission(ASSETS_READ))])
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

    storage = get_storage()
    return await storage.get_download_response(
        asset.filename, asset.filename, "application/octet-stream",
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


@router.get("/{asset_id}/preview", dependencies=[Depends(require_permission(ASSETS_READ))])
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

    # Admin preview always reads from local filesystem for speed
    return FileResponse(path=file_path, media_type=media_type)


@router.delete("/{asset_id}")
async def delete_asset(
    asset_id: uuid.UUID,
    user: User = Depends(require_permission(ASSETS_WRITE)),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    result = await db.execute(select(Asset).where(Asset.id == asset_id))
    asset = result.scalar_one_or_none()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")

    # For non-admins, check if user's group is linked via GroupAsset
    user_groups = await get_user_group_ids(user, db)
    if user_groups is not None:
        # Remove only the user's group references (ref-counted deletion)
        ga_result = await db.execute(
            select(GroupAsset).where(
                GroupAsset.asset_id == asset_id,
                GroupAsset.group_id.in_(user_groups),
            )
        )
        user_gas = ga_result.scalars().all()
        if not user_gas:
            raise HTTPException(status_code=403, detail="You don't have access to this asset")
        for ga in user_gas:
            await db.delete(ga)
        await db.commit()

        # Check if any GroupAsset references remain
        remaining = await db.scalar(
            select(func.count()).select_from(GroupAsset).where(GroupAsset.asset_id == asset_id)
        )
        if remaining > 0 or asset.is_global:
            return {"unlinked": asset.filename, "deleted": False}

    # Full deletion — no more references (or admin)
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
    storage = get_storage()
    if file_path.is_file():
        file_path.unlink()
    await storage.on_file_deleted(asset.filename)

    # Remove original file (if converted from HEIC/AVIF/etc)
    if asset.original_filename:
        orig_path = settings.asset_storage_path / "originals" / asset.original_filename
        if orig_path.is_file():
            orig_path.unlink()
        await storage.on_file_deleted(f"originals/{asset.original_filename}")

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
        await storage.on_file_deleted(f"variants/{variant.filename}")
        await db.delete(variant)

    # Remove all GroupAsset references
    ga_result = await db.execute(
        select(GroupAsset).where(GroupAsset.asset_id == asset_id)
    )
    for ga in ga_result.scalars().all():
        await db.delete(ga)

    await db.delete(asset)
    await db.commit()
    return {"deleted": asset.filename}


# ── Asset sharing & global toggle ──


@router.post("/{asset_id}/share", dependencies=[Depends(require_permission(ASSETS_WRITE))])
async def share_asset(
    asset_id: uuid.UUID,
    group_id: uuid.UUID = Query(..., description="Group UUID to share with"),
    db: AsyncSession = Depends(get_db),
):
    """Share an asset with an additional group."""
    asset = (await db.execute(select(Asset).where(Asset.id == asset_id))).scalar_one_or_none()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")

    # Check target group exists
    group = (await db.execute(select(DeviceGroup).where(DeviceGroup.id == group_id))).scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    # Check if already shared
    existing = (await db.execute(
        select(GroupAsset).where(GroupAsset.asset_id == asset_id, GroupAsset.group_id == group_id)
    )).scalar_one_or_none()
    if existing:
        return {"status": "already_shared"}

    db.add(GroupAsset(asset_id=asset_id, group_id=group_id, is_owner=False))
    await db.commit()
    return {"status": "shared", "asset_id": str(asset_id), "group_id": str(group_id)}


@router.delete("/{asset_id}/share", dependencies=[Depends(require_permission(ASSETS_WRITE))])
async def unshare_asset(
    asset_id: uuid.UUID,
    group_id: uuid.UUID = Query(..., description="Group UUID to unshare from"),
    db: AsyncSession = Depends(get_db),
):
    """Remove an asset's sharing with a group (cannot remove owner group)."""
    ga = (await db.execute(
        select(GroupAsset).where(GroupAsset.asset_id == asset_id, GroupAsset.group_id == group_id)
    )).scalar_one_or_none()
    if not ga:
        raise HTTPException(status_code=404, detail="Asset is not shared with this group")
    if ga.is_owner:
        raise HTTPException(status_code=409, detail="Cannot unshare from the owner group")
    await db.delete(ga)
    await db.commit()
    return {"status": "unshared", "asset_id": str(asset_id), "group_id": str(group_id)}


@router.post("/{asset_id}/global", dependencies=[Depends(require_permission(ASSETS_WRITE))])
async def toggle_asset_global(
    asset_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Toggle an asset's global visibility."""
    asset = (await db.execute(select(Asset).where(Asset.id == asset_id))).scalar_one_or_none()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")
    asset.is_global = not asset.is_global
    await db.commit()
    return {"is_global": asset.is_global}


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

    storage = get_storage()
    return await storage.get_download_response(
        f"variants/{variant.filename}", download_name, "application/octet-stream",
    )
