"""Device profile management API routes."""

import uuid
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from cms.auth import require_auth
from cms.database import get_db
from cms.models.asset import Asset, AssetType, AssetVariant, VariantStatus
from cms.models.device import Device
from cms.models.device_profile import DeviceProfile
from cms.schemas.profile import ProfileCreate, ProfileOut, ProfileUpdate
from cms.services.transcoder import enqueue_for_new_profile

router = APIRouter(prefix="/api/profiles", dependencies=[Depends(require_auth)])


@router.get("", response_model=List[ProfileOut])
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


@router.post("", response_model=ProfileOut, status_code=201)
async def create_profile(data: ProfileCreate, db: AsyncSession = Depends(get_db)):
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


@router.put("/{profile_id}", response_model=ProfileOut)
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

    # Detect whether any transcoding-relevant field actually changed
    transcode_changed = any(
        field in _TRANSCODE_FIELDS and getattr(profile, field) != value
        for field, value in updates.items()
    )

    for field, value in updates.items():
        setattr(profile, field, value)

    # Reset existing variants so they get re-transcoded
    if transcode_changed:
        var_result = await db.execute(
            select(AssetVariant).where(
                AssetVariant.profile_id == profile_id,
                AssetVariant.status.in_([VariantStatus.READY, VariantStatus.FAILED]),
            )
        )
        for variant in var_result.scalars().all():
            variant.status = VariantStatus.PENDING
            variant.progress = 0.0
            variant.error_message = ""

    await db.commit()
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


@router.delete("/{profile_id}")
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

    # Delete associated variants (cascade will handle, but clean files too)
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

    await db.delete(profile)
    await db.commit()
    return {"deleted": profile.name}


@router.get("/status")
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
