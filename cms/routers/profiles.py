"""Device profile management API routes."""

import uuid
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from cms.auth import require_auth, require_permission
from cms.database import get_db
from cms.permissions import PROFILES_READ, PROFILES_WRITE
from cms.models.asset import Asset, AssetType, AssetVariant, VariantStatus
from cms.models.device import Device
from cms.models.device_profile import DeviceProfile
from cms.schemas.profile import ProfileCreate, ProfileOut, ProfileUpdate
from cms.services.transcoder import cancel_profile_transcodes, enqueue_for_new_profile, notify_worker

router = APIRouter(prefix="/api/profiles", dependencies=[Depends(require_auth)])

# ── Built-in profile canonical defaults ──────────────────────────────
# Used by _seed_profiles (startup) and the reset-to-defaults endpoint.

BUILTIN_PROFILES = {
    "pi-zero-2w": {
        "description": "Raspberry Pi Zero 2 W — H.264 Main, 1080p30",
        "video_codec": "h264",
        "video_profile": "main",
        "max_width": 1920,
        "max_height": 1080,
        "max_fps": 30,
        "crf": 23,
        "video_bitrate": "",
        "pixel_format": "auto",
        "color_space": "auto",
        "audio_codec": "aac",
        "audio_bitrate": "128k",
    },
    "pi-4": {
        "description": "Raspberry Pi 4 — HEVC Main, 1080p30",
        "video_codec": "h265",
        "video_profile": "main",
        "max_width": 1920,
        "max_height": 1080,
        "max_fps": 30,
        "crf": 23,
        "video_bitrate": "",
        "pixel_format": "auto",
        "color_space": "auto",
        "audio_codec": "aac",
        "audio_bitrate": "128k",
    },
    "pi-5": {
        "description": "Raspberry Pi 5 / CM5 — HEVC Main, 1080p60",
        "video_codec": "h265",
        "video_profile": "main",
        "max_width": 1920,
        "max_height": 1080,
        "max_fps": 60,
        "crf": 23,
        "video_bitrate": "",
        "pixel_format": "auto",
        "color_space": "auto",
        "audio_codec": "aac",
        "audio_bitrate": "128k",
    },
}

# ── Codec/profile compatibility rules ────────────────────────────────

# Profiles restricted to 4:2:0 chroma subsampling
_PROFILES_420_ONLY: dict[str, set[str]] = {
    "h264": {"baseline", "main", "high", "high10"},
    "h265": {"main", "main10"},
}

# 8-bit-only profiles: cannot use 10-bit pixel formats or HDR color spaces
_PROFILES_8BIT_ONLY: dict[str, set[str]] = {
    "h264": {"baseline", "main", "high"},
    "h265": {"main"},
}

_PIX_FMT_422_OR_444 = {"yuv422p", "yuv422p10le", "yuv444p", "yuv444p10le"}
_PIX_FMT_10BIT = {"yuv420p10le", "yuv422p10le", "yuv444p10le"}
_AV1_ALLOWED_PIX_FMT = {"auto", "yuv420p", "yuv420p10le"}
_HDR_COLOR_SPACES = {"bt2020-pq", "bt2020-hlg"}


def _validate_profile_compat(
    video_codec: str, video_profile: str,
    pixel_format: str, color_space: str,
) -> None:
    """Raise HTTPException 422 if pixel_format or color_space is
    incompatible with the codec/profile combination."""
    forces_420 = (
        video_codec == "av1"
        or video_profile in _PROFILES_420_ONLY.get(video_codec, set())
    )
    is_8bit_only = video_profile in _PROFILES_8BIT_ONLY.get(video_codec, set())

    # Pixel format checks
    if pixel_format != "auto":
        if video_codec == "av1" and pixel_format not in _AV1_ALLOWED_PIX_FMT:
            raise HTTPException(
                status_code=422,
                detail=f"AV1 only supports pixel formats: {', '.join(sorted(_AV1_ALLOWED_PIX_FMT - {'auto'}))}",
            )
        if forces_420 and pixel_format in _PIX_FMT_422_OR_444:
            raise HTTPException(
                status_code=422,
                detail=f"{video_codec}/{video_profile} only supports 4:2:0 pixel formats",
            )
        if is_8bit_only and pixel_format in _PIX_FMT_10BIT:
            raise HTTPException(
                status_code=422,
                detail=f"{video_codec}/{video_profile} is 8-bit only — 10-bit pixel formats are not supported",
            )

    # Color space checks
    if color_space in _HDR_COLOR_SPACES and is_8bit_only:
        raise HTTPException(
            status_code=422,
            detail=f"{video_codec}/{video_profile} is 8-bit only — HDR color space '{color_space}' requires a 10-bit profile",
        )


@router.get("", response_model=List[ProfileOut], dependencies=[Depends(require_permission(PROFILES_READ))])
async def list_profiles(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(DeviceProfile).order_by(DeviceProfile.name)
    )
    profiles = result.scalars().all()

    # Annotate with device count and variant stats
    out = []
    for p in profiles:
        dev_count = await db.execute(
            select(func.count(Device.id)).where(Device.profile_id == p.id)
        )
        total_var = await db.execute(
            select(func.count(AssetVariant.id)).where(AssetVariant.profile_id == p.id)
        )
        ready_var = await db.execute(
            select(func.count(AssetVariant.id)).where(
                AssetVariant.profile_id == p.id,
                AssetVariant.status == VariantStatus.READY,
            )
        )
        out.append(ProfileOut(
            id=p.id,
            name=p.name,
            description=p.description,
            video_codec=p.video_codec,
            video_profile=p.video_profile,
            max_width=p.max_width,
            max_height=p.max_height,
            max_fps=p.max_fps,
            video_bitrate=p.video_bitrate,
            crf=p.crf,
            pixel_format=p.pixel_format,
            color_space=p.color_space,
            audio_codec=p.audio_codec,
            audio_bitrate=p.audio_bitrate,
            builtin=p.builtin,
            device_count=dev_count.scalar() or 0,
            total_variants=total_var.scalar() or 0,
            ready_variants=ready_var.scalar() or 0,
            created_at=p.created_at,
        ))
    return out


@router.post("", response_model=ProfileOut, status_code=201, dependencies=[Depends(require_permission(PROFILES_WRITE))])
async def create_profile(data: ProfileCreate, db: AsyncSession = Depends(get_db)):
    # Validate codec/profile compatibility
    _validate_profile_compat(
        data.video_codec, data.video_profile,
        data.pixel_format, data.color_space,
    )

    # Check duplicate name
    existing = await db.execute(
        select(DeviceProfile).where(DeviceProfile.name == data.name)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Profile name already exists")

    profile = DeviceProfile(**data.model_dump())
    db.add(profile)
    await db.commit()
    await db.refresh(profile)

    # Enqueue transcoding for all existing video assets
    count = await enqueue_for_new_profile(profile.id, db)
    if count:
        await notify_worker(db)

    return ProfileOut(
        id=profile.id,
        name=profile.name,
        description=profile.description,
        video_codec=profile.video_codec,
        video_profile=profile.video_profile,
        max_width=profile.max_width,
        max_height=profile.max_height,
        max_fps=profile.max_fps,
        video_bitrate=profile.video_bitrate,
        crf=profile.crf,
        pixel_format=profile.pixel_format,
        color_space=profile.color_space,
        audio_codec=profile.audio_codec,
        audio_bitrate=profile.audio_bitrate,
        builtin=profile.builtin,
        device_count=0,
        total_variants=count,
        ready_variants=0,
        created_at=profile.created_at,
    )


# Fields that affect transcoding output — changes require re-encoding variants
_TRANSCODE_FIELDS = {
    "video_profile", "max_width", "max_height", "max_fps",
    "crf", "video_bitrate", "pixel_format", "color_space",
    "audio_codec", "audio_bitrate",
}


@router.put("/{profile_id}", response_model=ProfileOut, dependencies=[Depends(require_permission(PROFILES_WRITE))])
async def update_profile(
    profile_id: uuid.UUID,
    data: ProfileUpdate,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(DeviceProfile).where(DeviceProfile.id == profile_id)
    )
    profile = result.scalar_one_or_none()
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")

    updates = data.model_dump(exclude_unset=True)

    # Validate codec/profile compatibility with the merged state
    _validate_profile_compat(
        video_codec=profile.video_codec,  # codec is immutable
        video_profile=updates.get("video_profile", profile.video_profile),
        pixel_format=updates.get("pixel_format", profile.pixel_format),
        color_space=updates.get("color_space", profile.color_space),
    )

    # Detect whether any transcoding-relevant field actually changed
    transcode_changed = any(
        field in _TRANSCODE_FIELDS and getattr(profile, field) != value
        for field, value in updates.items()
    )

    for field, value in updates.items():
        setattr(profile, field, value)

    # Reset existing variants so they get re-transcoded
    if transcode_changed:
        # Kill any in-progress ffmpeg for this profile
        cancel_profile_transcodes(profile_id)

        var_result = await db.execute(
            select(AssetVariant).where(
                AssetVariant.profile_id == profile_id,
                AssetVariant.status.in_([
                    VariantStatus.READY,
                    VariantStatus.FAILED,
                    VariantStatus.PROCESSING,
                ]),
            )
        )
        for variant in var_result.scalars().all():
            variant.status = VariantStatus.PENDING
            variant.progress = 0.0
            variant.error_message = ""

    await db.commit()

    # Notify worker if variants were reset to PENDING
    if transcode_changed:
        await notify_worker(db)

    await db.refresh(profile)

    dev_count = await db.execute(
        select(func.count(Device.id)).where(Device.profile_id == profile.id)
    )
    total_var = await db.execute(
        select(func.count(AssetVariant.id)).where(AssetVariant.profile_id == profile.id)
    )
    ready_var = await db.execute(
        select(func.count(AssetVariant.id)).where(
            AssetVariant.profile_id == profile.id,
            AssetVariant.status == VariantStatus.READY,
        )
    )

    return ProfileOut(
        id=profile.id,
        name=profile.name,
        description=profile.description,
        video_codec=profile.video_codec,
        video_profile=profile.video_profile,
        max_width=profile.max_width,
        max_height=profile.max_height,
        max_fps=profile.max_fps,
        video_bitrate=profile.video_bitrate,
        crf=profile.crf,
        pixel_format=profile.pixel_format,
        color_space=profile.color_space,
        audio_codec=profile.audio_codec,
        audio_bitrate=profile.audio_bitrate,
        builtin=profile.builtin,
        device_count=dev_count.scalar() or 0,
        total_variants=total_var.scalar() or 0,
        ready_variants=ready_var.scalar() or 0,
        created_at=profile.created_at,
    )


@router.delete("/{profile_id}", dependencies=[Depends(require_permission(PROFILES_WRITE))])
async def delete_profile(profile_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(DeviceProfile).where(DeviceProfile.id == profile_id)
    )
    profile = result.scalar_one_or_none()
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    if profile.builtin:
        raise HTTPException(status_code=400, detail="Cannot delete built-in profile")

    # Check if devices use this profile
    dev_count = await db.execute(
        select(func.count(Device.id)).where(Device.profile_id == profile.id)
    )
    if (dev_count.scalar() or 0) > 0:
        raise HTTPException(
            status_code=400,
            detail="Cannot delete profile with assigned devices",
        )

    # Cancel any active transcode for this profile
    from cms.services.transcoder import cancel_profile_transcodes
    cancel_profile_transcodes(profile_id)

    # Delete associated variants (files + DB rows) before removing profile
    from cms.auth import get_settings
    settings = get_settings()
    variants_dir = settings.asset_storage_path / "variants"

    var_result = await db.execute(
        select(AssetVariant).where(AssetVariant.profile_id == profile_id)
    )
    for variant in var_result.scalars().all():
        vpath = variants_dir / variant.filename
        if vpath.is_file():
            vpath.unlink()
        await db.delete(variant)

    await db.delete(profile)
    await db.commit()
    return {"deleted": profile.name}


@router.post("/{profile_id}/copy", response_model=ProfileOut, status_code=201, dependencies=[Depends(require_permission(PROFILES_WRITE))])
async def copy_profile(
    profile_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(DeviceProfile).where(DeviceProfile.id == profile_id)
    )
    source = result.scalar_one_or_none()
    if not source:
        raise HTTPException(status_code=404, detail="Profile not found")

    # Generate a unique copy name
    base_name = f"Copy of {source.name}"
    copy_name = base_name
    suffix = 2
    while True:
        dup = await db.execute(
            select(DeviceProfile).where(DeviceProfile.name == copy_name)
        )
        if not dup.scalar_one_or_none():
            break
        copy_name = f"{base_name} {suffix}"
        suffix += 1

    profile = DeviceProfile(
        name=copy_name,
        description=source.description,
        video_codec=source.video_codec,
        video_profile=source.video_profile,
        max_width=source.max_width,
        max_height=source.max_height,
        max_fps=source.max_fps,
        video_bitrate=source.video_bitrate,
        crf=source.crf,
        pixel_format=source.pixel_format,
        color_space=source.color_space,
        audio_codec=source.audio_codec,
        audio_bitrate=source.audio_bitrate,
        builtin=False,
    )
    db.add(profile)
    await db.commit()
    await db.refresh(profile)

    # Enqueue transcoding for all existing video assets
    count = await enqueue_for_new_profile(profile.id, db)
    if count:
        await notify_worker(db)

    return ProfileOut(
        id=profile.id,
        name=profile.name,
        description=profile.description,
        video_codec=profile.video_codec,
        video_profile=profile.video_profile,
        max_width=profile.max_width,
        max_height=profile.max_height,
        max_fps=profile.max_fps,
        video_bitrate=profile.video_bitrate,
        crf=profile.crf,
        pixel_format=profile.pixel_format,
        color_space=profile.color_space,
        audio_codec=profile.audio_codec,
        audio_bitrate=profile.audio_bitrate,
        builtin=profile.builtin,
        device_count=0,
        total_variants=count,
        ready_variants=0,
        created_at=profile.created_at,
    )


@router.post("/{profile_id}/reset", response_model=ProfileOut, dependencies=[Depends(require_permission(PROFILES_WRITE))])
async def reset_profile(
    profile_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Reset a built-in profile to its canonical default values."""
    result = await db.execute(
        select(DeviceProfile).where(DeviceProfile.id == profile_id)
    )
    profile = result.scalar_one_or_none()
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    if not profile.builtin:
        raise HTTPException(status_code=400, detail="Only built-in profiles can be reset")
    if profile.name not in BUILTIN_PROFILES:
        raise HTTPException(status_code=400, detail="No defaults found for this profile")

    defaults = BUILTIN_PROFILES[profile.name]

    # Detect whether any transcoding-relevant field will change
    transcode_changed = any(
        field in _TRANSCODE_FIELDS and getattr(profile, field) != value
        for field, value in defaults.items()
    )

    # Apply defaults
    for field, value in defaults.items():
        setattr(profile, field, value)

    # Reset variants if transcoding fields changed
    if transcode_changed:
        cancel_profile_transcodes(profile_id)
        var_result = await db.execute(
            select(AssetVariant).where(
                AssetVariant.profile_id == profile_id,
                AssetVariant.status.in_([
                    VariantStatus.READY,
                    VariantStatus.FAILED,
                    VariantStatus.PROCESSING,
                ]),
            )
        )
        for variant in var_result.scalars().all():
            variant.status = VariantStatus.PENDING
            variant.progress = 0.0
            variant.error_message = ""

    await db.commit()

    if transcode_changed:
        await notify_worker(db)

    await db.refresh(profile)

    dev_count = await db.execute(
        select(func.count(Device.id)).where(Device.profile_id == profile.id)
    )
    total_var = await db.execute(
        select(func.count(AssetVariant.id)).where(AssetVariant.profile_id == profile.id)
    )
    ready_var = await db.execute(
        select(func.count(AssetVariant.id)).where(
            AssetVariant.profile_id == profile.id,
            AssetVariant.status == VariantStatus.READY,
        )
    )

    return ProfileOut(
        id=profile.id,
        name=profile.name,
        description=profile.description,
        video_codec=profile.video_codec,
        video_profile=profile.video_profile,
        max_width=profile.max_width,
        max_height=profile.max_height,
        max_fps=profile.max_fps,
        video_bitrate=profile.video_bitrate,
        crf=profile.crf,
        pixel_format=profile.pixel_format,
        color_space=profile.color_space,
        audio_codec=profile.audio_codec,
        audio_bitrate=profile.audio_bitrate,
        builtin=profile.builtin,
        device_count=dev_count.scalar() or 0,
        total_variants=total_var.scalar() or 0,
        ready_variants=ready_var.scalar() or 0,
        created_at=profile.created_at,
    )


@router.get("/status", dependencies=[Depends(require_permission(PROFILES_READ))])
async def profiles_status_json(db: AsyncSession = Depends(get_db)):
    """Lightweight JSON for profiles page polling — queue status + profile variant counts."""

    # Profile variant summaries
    result = await db.execute(
        select(DeviceProfile).order_by(DeviceProfile.name)
    )
    profiles_out = []
    for p in result.scalars().all():
        total_var = (await db.execute(
            select(func.count(AssetVariant.id)).where(AssetVariant.profile_id == p.id)
        )).scalar() or 0
        ready_var = (await db.execute(
            select(func.count(AssetVariant.id)).where(
                AssetVariant.profile_id == p.id,
                AssetVariant.status == VariantStatus.READY,
            )
        )).scalar() or 0
        profiles_out.append({
            "id": str(p.id),
            "total_variants": total_var,
            "ready_variants": ready_var,
        })

    # Transcode queue (pending / processing / failed)
    queue_result = await db.execute(
        select(AssetVariant)
        .where(AssetVariant.status.in_([VariantStatus.PENDING, VariantStatus.PROCESSING, VariantStatus.FAILED]))
        .order_by(AssetVariant.created_at)
        .limit(50)
    )
    queue_variants = queue_result.scalars().all()

    queue_out = []
    for v in queue_variants:
        await db.refresh(v, ["source_asset", "profile"])
        queue_out.append({
            "id": str(v.id),
            "source_filename": v.source_asset.filename if v.source_asset else "?",
            "profile_name": v.profile.name if v.profile else "?",
            "status": v.status.value,
            "progress": v.progress,
            "error_message": v.error_message or "",
        })

    return {
        "profiles": profiles_out,
        "queue": queue_out,
        "queue_count": len(queue_out),
    }
