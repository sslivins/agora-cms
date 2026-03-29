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
        audio_codec=profile.audio_codec,
        audio_bitrate=profile.audio_bitrate,
        builtin=profile.builtin,
        device_count=0,
        total_variants=count,
        ready_variants=0,
        created_at=profile.created_at,
    )


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

    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(profile, field, value)

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
