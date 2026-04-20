"""Device profile management API routes."""

import logging
import uuid
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from cms.auth import require_auth, require_permission
from cms.database import get_db
from cms.permissions import PROFILES_READ, PROFILES_WRITE
from cms.models.asset import Asset, AssetType, AssetVariant, VariantStatus
from cms.models.device import Device
from cms.models.device_profile import DeviceProfile
from cms.profile_defaults import BUILTIN_PROFILES
from cms.schemas.profile import ProfileCreate, ProfileOut, ProfileUpdate
from cms.services.transcoder import (
    cancel_profile_transcodes,
    enqueue_for_new_profile,
    enqueue_variants,
    flag_profile_jobs_cancelled,
    supersede_profile_variants,
)
from cms.services.audit_service import audit_log, compute_diff

logger = logging.getLogger("agora.cms.profiles")

router = APIRouter(prefix="/api/profiles", dependencies=[Depends(require_auth)])

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
            enabled=p.enabled,
            device_count=dev_count.scalar() or 0,
            total_variants=total_var.scalar() or 0,
            ready_variants=ready_var.scalar() or 0,
            matches_defaults=_matches_defaults(p),
            created_at=p.created_at,
        ))
    return out


@router.post("", response_model=ProfileOut, status_code=201, dependencies=[Depends(require_permission(PROFILES_WRITE))])
async def create_profile(data: ProfileCreate, request: Request, db: AsyncSession = Depends(get_db)):
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
    await db.flush()
    await audit_log(
        db, user=getattr(request.state, "user", None),
        action="profile.create", resource_type="profile",
        resource_id=str(profile.id),
        description=f"Created transcode profile '{profile.name}'",
        details={"name": profile.name, "video_codec": profile.video_codec},
        request=request,
    )
    await db.commit()
    await db.refresh(profile)

    # Enqueue transcoding for all existing video assets
    variant_ids = await enqueue_for_new_profile(profile.id, db)
    if variant_ids:
        await enqueue_variants(db, variant_ids)

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
        enabled=profile.enabled,
        device_count=0,
        total_variants=len(variant_ids),
        ready_variants=0,
        matches_defaults=_matches_defaults(profile),
        created_at=profile.created_at,
    )


# Fields that affect transcoding output— changes require re-encoding variants
_TRANSCODE_FIELDS = {
    "video_codec", "video_profile", "max_width", "max_height", "max_fps",
    "crf", "video_bitrate", "pixel_format", "color_space",
    "audio_codec", "audio_bitrate",
}


def _matches_defaults(profile: DeviceProfile) -> bool:
    """True iff profile is a built-in whose transcoding-relevant fields
    all still match the canonical factory defaults.  Description is
    intentionally excluded — editing only the description should not
    enable the Reset button.
    """
    if not profile.builtin or profile.name not in BUILTIN_PROFILES:
        return False
    defaults = BUILTIN_PROFILES[profile.name]
    return all(
        getattr(profile, field) == value
        for field, value in defaults.items()
        if field in _TRANSCODE_FIELDS
    )


async def _annotate_profile(p: DeviceProfile, db: AsyncSession) -> dict:
    """Wrap a DeviceProfile with the computed counts the profile_row
    macro needs (device_count, total_variants, ready_variants,
    matches_defaults).  Used by the fragment-row endpoint so the
    template rendered for polling stays byte-identical to the one
    rendered for the full page.
    """
    dev_count = (await db.execute(
        select(func.count(Device.id)).where(Device.profile_id == p.id)
    )).scalar() or 0
    total_var = (await db.execute(
        select(func.count(AssetVariant.id)).where(AssetVariant.profile_id == p.id)
    )).scalar() or 0
    ready_var = (await db.execute(
        select(func.count(AssetVariant.id)).where(
            AssetVariant.profile_id == p.id,
            AssetVariant.status == VariantStatus.READY,
        )
    )).scalar() or 0

    class _Annotated:
        pass

    a = _Annotated()
    for col in (
        "id", "name", "description", "video_codec", "video_profile",
        "max_width", "max_height", "max_fps", "video_bitrate", "crf",
        "pixel_format", "color_space", "audio_codec", "audio_bitrate",
        "builtin", "enabled",
    ):
        setattr(a, col, getattr(p, col))
    a.device_count = dev_count
    a.total_variants = total_var
    a.ready_variants = ready_var
    a.matches_defaults = _matches_defaults(p)
    return a


@router.get("/{profile_id}/row", dependencies=[Depends(require_permission(PROFILES_READ))])
async def get_profile_row(profile_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)):
    """Return just the rendered <tr> HTML for a single profile.

    Used by the no-reload flows on the profiles page (create, edit,
    copy, reset, and the cross-replica poller) so the client never has
    to synthesize row markup in JS. See issue #87.
    """
    from fastapi.responses import HTMLResponse
    from cms.ui import templates

    profile = (await db.execute(
        select(DeviceProfile).where(DeviceProfile.id == profile_id)
    )).scalar_one_or_none()
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")

    user = getattr(request.state, "user", None)
    user_perms = list(user.role.permissions) if user and user.role else []

    annotated = await _annotate_profile(profile, db)
    macros = templates.env.get_template("_macros.html").module
    html = macros.profile_row(annotated, user_perms)
    return HTMLResponse(str(html))


@router.put("/{profile_id}", response_model=ProfileOut, dependencies=[Depends(require_permission(PROFILES_WRITE))])
async def update_profile(
    profile_id: uuid.UUID,
    data: ProfileUpdate,
    request: Request,
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
        video_codec=updates.get("video_codec", profile.video_codec),
        video_profile=updates.get("video_profile", profile.video_profile),
        pixel_format=updates.get("pixel_format", profile.pixel_format),
        color_space=updates.get("color_space", profile.color_space),
    )

    # Snapshot before mutation so we can build a true diff for the audit log
    changes = compute_diff(profile, updates)

    # Detect whether any transcoding-relevant field actually changed
    transcode_changed = any(field in _TRANSCODE_FIELDS for field in changes)

    for field, value in updates.items():
        setattr(profile, field, value)

    # Reset existing variants so they get re-transcoded
    new_variant_ids: list[uuid.UUID] = []
    cancelled_jobs = 0
    if transcode_changed:
        logger.info(
            "update_profile: profile %s (%s) transcoding fields changed (%s); "
            "superseding variants",
            profile_id, profile.name,
            sorted(f for f in changes if f in _TRANSCODE_FIELDS),
        )
        # Flag any in-flight worker jobs for cooperative cancellation.
        # Workers will SIGTERM ffmpeg on the next heartbeat tick.  The count
        # is logged for observability but not surfaced in the audit log —
        # it's an implementation detail, not a user-meaningful action.
        cancelled_jobs = await flag_profile_jobs_cancelled(db, profile_id)
        cancel_profile_transcodes(profile_id)

        # Create brand-new variant rows (fresh UUIDs → fresh blob paths) for
        # every asset that currently has a live variant under this profile.
        # The OLD variant rows are left in place so devices keep playing the
        # last known good blob; the reaper will soft-delete them once a new
        # READY sibling exists, then hard-delete once jobs are terminal.
        new_variant_ids = await supersede_profile_variants(
            db, profile_id, changed_fields=set(changes),
        )

    reset_count = len(new_variant_ids)

    await audit_log(
        db, user=getattr(request.state, "user", None),
        action="profile.update", resource_type="profile",
        resource_id=str(profile_id),
        description=f"Modified transcode profile '{profile.name}'",
        details={
            "changes": changes,
            "transcode_changed": transcode_changed,
            "variants_superseded": reset_count,
        },
        request=request,
    )
    await db.commit()

    # Enqueue jobs for the newly-created PENDING variants
    if transcode_changed and new_variant_ids:
        await enqueue_variants(db, new_variant_ids)
        logger.info(
            "update_profile: profile %s — enqueued %d VARIANT_TRANSCODE job(s) "
            "after superseding (cancelled %d in-flight)",
            profile_id, len(new_variant_ids), cancelled_jobs,
        )

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
        enabled=profile.enabled,
        device_count=dev_count.scalar() or 0,
        total_variants=total_var.scalar() or 0,
        ready_variants=ready_var.scalar() or 0,
        matches_defaults=_matches_defaults(profile),
        created_at=profile.created_at,
    )


@router.delete("/{profile_id}", dependencies=[Depends(require_permission(PROFILES_WRITE))])
async def delete_profile(profile_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)):
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

    # Flag any in-flight worker jobs for cooperative cancellation.
    await flag_profile_jobs_cancelled(db, profile_id)
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

    profile_name = profile.name
    await audit_log(
        db, user=getattr(request.state, "user", None),
        action="profile.delete", resource_type="profile",
        resource_id=str(profile_id),
        description=f"Deleted transcode profile '{profile_name}'",
        details={"name": profile_name},
        request=request,
    )
    await db.delete(profile)
    await db.commit()
    return {"deleted": profile_name}


@router.post("/{profile_id}/copy", response_model=ProfileOut, status_code=201, dependencies=[Depends(require_permission(PROFILES_WRITE))])
async def copy_profile(
    profile_id: uuid.UUID,
    request: Request,
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
    await db.flush()
    await audit_log(
        db, user=getattr(request.state, "user", None),
        action="profile.copy", resource_type="profile",
        resource_id=str(profile.id),
        description=f"Copied transcode profile '{source.name}' → '{profile.name}'",
        details={"source_id": str(source.id), "source_name": source.name, "name": profile.name},
        request=request,
    )
    await db.commit()
    await db.refresh(profile)

    # Enqueue transcoding for all existing video assets
    variant_ids = await enqueue_for_new_profile(profile.id, db)
    if variant_ids:
        await enqueue_variants(db, variant_ids)

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
        enabled=profile.enabled,
        device_count=0,
        total_variants=len(variant_ids),
        ready_variants=0,
        matches_defaults=_matches_defaults(profile),
        created_at=profile.created_at,
    )


@router.post("/{profile_id}/reset", response_model=ProfileOut, dependencies=[Depends(require_permission(PROFILES_WRITE))])
async def reset_profile(
    profile_id: uuid.UUID,
    request: Request,
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

    # Detect which transcoding-relevant fields will change.  We keep the
    # set so we can tell supersede_profile_variants whether IMAGE assets
    # need to be re-rendered (only max_width/max_height affect them).
    reset_changed_fields: set[str] = {
        field for field, value in defaults.items()
        if field in _TRANSCODE_FIELDS and getattr(profile, field) != value
    }
    transcode_changed = bool(reset_changed_fields)

    # Apply defaults
    for field, value in defaults.items():
        setattr(profile, field, value)

    # Reset variants if transcoding fields changed
    new_variant_ids: list[uuid.UUID] = []
    cancelled_jobs = 0
    if transcode_changed:
        logger.info(
            "reset_profile: built-in profile %s (%s) reset to defaults — "
            "superseding variants", profile_id, profile.name,
        )
        cancelled_jobs = await flag_profile_jobs_cancelled(db, profile_id)
        cancel_profile_transcodes(profile_id)
        new_variant_ids = await supersede_profile_variants(
            db, profile_id, changed_fields=reset_changed_fields,
        )

    reset_count = len(new_variant_ids)

    await audit_log(
        db, user=getattr(request.state, "user", None),
        action="profile.reset", resource_type="profile",
        resource_id=str(profile_id),
        description=f"Reset built-in transcode profile '{profile.name}' to defaults",
        details={
            "name": profile.name,
            "variants_superseded": reset_count,
        },
        request=request,
    )
    await db.commit()

    if transcode_changed and new_variant_ids:
        await enqueue_variants(db, new_variant_ids)
        logger.info(
            "reset_profile: profile %s — enqueued %d VARIANT_TRANSCODE job(s) "
            "after superseding (cancelled %d in-flight)",
            profile_id, len(new_variant_ids), cancelled_jobs,
        )

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
        enabled=profile.enabled,
        device_count=dev_count.scalar() or 0,
        total_variants=total_var.scalar() or 0,
        ready_variants=ready_var.scalar() or 0,
        matches_defaults=_matches_defaults(profile),
        created_at=profile.created_at,
    )


@router.post("/{profile_id}/disable", response_model=ProfileOut, dependencies=[Depends(require_permission(PROFILES_WRITE))])
async def disable_profile(
    profile_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Disable a profile.

    Stops new variants from being generated for this profile on asset
    upload / new-profile fan-out. Any pending or in-flight transcode
    jobs for this profile are cancelled. Existing READY variants are
    preserved so re-enabling is instant.
    """
    result = await db.execute(
        select(DeviceProfile).where(DeviceProfile.id == profile_id)
    )
    profile = result.scalar_one_or_none()
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")

    if not profile.enabled:
        # Idempotent — return current state
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
            id=profile.id, name=profile.name, description=profile.description,
            video_codec=profile.video_codec, video_profile=profile.video_profile,
            max_width=profile.max_width, max_height=profile.max_height,
            max_fps=profile.max_fps, video_bitrate=profile.video_bitrate,
            crf=profile.crf, pixel_format=profile.pixel_format,
            color_space=profile.color_space, audio_codec=profile.audio_codec,
            audio_bitrate=profile.audio_bitrate, builtin=profile.builtin,
            enabled=profile.enabled,
            device_count=dev_count.scalar() or 0,
            total_variants=total_var.scalar() or 0,
            ready_variants=ready_var.scalar() or 0,
            matches_defaults=_matches_defaults(profile),
            created_at=profile.created_at,
        )

    profile.enabled = False

    # Cancel pending/in-flight transcode work for this profile.
    cancelled_jobs = await flag_profile_jobs_cancelled(db, profile_id)
    cancel_profile_transcodes(profile_id)

    await audit_log(
        db, user=getattr(request.state, "user", None),
        action="profile.disable", resource_type="profile",
        resource_id=str(profile_id),
        description=f"Disabled transcode profile '{profile.name}'",
        details={"name": profile.name, "cancelled_jobs": cancelled_jobs},
        request=request,
    )
    await db.commit()
    await db.refresh(profile)

    logger.info(
        "disable_profile: profile %s (%s) disabled — cancelled %d job(s)",
        profile_id, profile.name, cancelled_jobs,
    )

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
        id=profile.id, name=profile.name, description=profile.description,
        video_codec=profile.video_codec, video_profile=profile.video_profile,
        max_width=profile.max_width, max_height=profile.max_height,
        max_fps=profile.max_fps, video_bitrate=profile.video_bitrate,
        crf=profile.crf, pixel_format=profile.pixel_format,
        color_space=profile.color_space, audio_codec=profile.audio_codec,
        audio_bitrate=profile.audio_bitrate, builtin=profile.builtin,
        enabled=profile.enabled,
        device_count=dev_count.scalar() or 0,
        total_variants=total_var.scalar() or 0,
        ready_variants=ready_var.scalar() or 0,
        matches_defaults=_matches_defaults(profile),
        created_at=profile.created_at,
    )


@router.post("/{profile_id}/enable", response_model=ProfileOut, dependencies=[Depends(require_permission(PROFILES_WRITE))])
async def enable_profile(
    profile_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Re-enable a profile.

    Re-enqueues transcoding for any assets that don't yet have a variant
    for this profile (covers assets uploaded while the profile was
    disabled). Existing variants are preserved.
    """
    result = await db.execute(
        select(DeviceProfile).where(DeviceProfile.id == profile_id)
    )
    profile = result.scalar_one_or_none()
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")

    was_disabled = not profile.enabled
    profile.enabled = True
    await db.commit()
    await db.refresh(profile)

    new_variant_ids: list[uuid.UUID] = []
    if was_disabled:
        # Re-run fan-out: any assets uploaded while disabled will now
        # get variants; assets that already have variants are no-ops.
        new_variant_ids = await enqueue_for_new_profile(profile.id, db)
        if new_variant_ids:
            await enqueue_variants(db, new_variant_ids)

        await audit_log(
            db, user=getattr(request.state, "user", None),
            action="profile.enable", resource_type="profile",
            resource_id=str(profile_id),
            description=f"Enabled transcode profile '{profile.name}'",
            details={
                "name": profile.name,
                "variants_enqueued": len(new_variant_ids),
            },
            request=request,
        )
        await db.commit()
        logger.info(
            "enable_profile: profile %s (%s) enabled — enqueued %d variant(s)",
            profile_id, profile.name, len(new_variant_ids),
        )

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
        id=profile.id, name=profile.name, description=profile.description,
        video_codec=profile.video_codec, video_profile=profile.video_profile,
        max_width=profile.max_width, max_height=profile.max_height,
        max_fps=profile.max_fps, video_bitrate=profile.video_bitrate,
        crf=profile.crf, pixel_format=profile.pixel_format,
        color_space=profile.color_space, audio_codec=profile.audio_codec,
        audio_bitrate=profile.audio_bitrate, builtin=profile.builtin,
        enabled=profile.enabled,
        device_count=dev_count.scalar() or 0,
        total_variants=total_var.scalar() or 0,
        ready_variants=ready_var.scalar() or 0,
        matches_defaults=_matches_defaults(profile),
        created_at=profile.created_at,
    )


@router.get("/status")
async def profiles_status_json(
    user=Depends(require_permission(PROFILES_READ)),
    db: AsyncSession = Depends(get_db),
):
    """Lightweight JSON for profiles page polling — queue status + profile variant counts.

    Queue entries are filtered to assets the caller is permitted to see (to avoid
    leaking filenames of group-scoped assets to non-admin users).
    """
    from cms.routers.assets import _visible_asset_ids

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
            "matches_defaults": _matches_defaults(p),
        })

    # Transcode queue (pending / processing / failed) — scoped to visible assets
    visible = await _visible_asset_ids(user, db)
    queue_q = (
        select(AssetVariant)
        .where(AssetVariant.status.in_([VariantStatus.PENDING, VariantStatus.PROCESSING, VariantStatus.FAILED]))
        .order_by(AssetVariant.created_at)
        .limit(50)
    )
    if visible is not None:
        if not visible:
            queue_variants = []
        else:
            queue_q = queue_q.where(AssetVariant.source_asset_id.in_(visible))
            queue_variants = (await db.execute(queue_q)).scalars().all()
    else:
        queue_variants = (await db.execute(queue_q)).scalars().all()

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
