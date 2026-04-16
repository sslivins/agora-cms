"""Asset library API routes with RBAC group scoping."""

import hashlib
import logging
import re
import uuid
from pathlib import Path
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile
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

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/assets", dependencies=[Depends(require_auth)])


# ── Device download auth ──


def _hash_device_key(key: str) -> str:
    """SHA-256 hash matching the scheme used by ws.py for device API keys."""
    return hashlib.sha256(key.encode()).hexdigest()


# Grace period after key rotation during which the previous key is still accepted.
# This covers in-flight downloads that started before the device received its new key.
_KEY_GRACE_SECONDS = 300  # 5 minutes


async def require_device_or_session_auth(
    request: Request,
    key: str | None = Query(None, alias="key"),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> None:
    """Allow access if the request carries a valid device API key OR
    an authenticated browser session (cookie).

    Device key is accepted via ``X-Device-API-Key`` header or ``?key=`` query param.
    During key rotation, the previous key is accepted for a short grace period.
    """
    from datetime import datetime, timedelta, timezone as tz

    # 1. Try device API key (header first, then query param)
    api_key = request.headers.get("X-Device-API-Key") or key
    if api_key:
        key_hash = _hash_device_key(api_key)

        # Check current key
        result = await db.execute(
            select(Device.id).where(Device.device_api_key_hash == key_hash)
        )
        if result.scalar_one_or_none() is not None:
            return  # valid current key

        # Check previous key within grace period
        result = await db.execute(
            select(Device).where(Device.previous_api_key_hash == key_hash)
        )
        device = result.scalar_one_or_none()
        if device is not None and device.api_key_rotated_at is not None:
            rotated_at = device.api_key_rotated_at
            # Ensure timezone-aware comparison (SQLite returns naïve datetimes)
            if rotated_at.tzinfo is None:
                rotated_at = rotated_at.replace(tzinfo=tz.utc)
            age = datetime.now(tz.utc) - rotated_at
            if age < timedelta(seconds=_KEY_GRACE_SECONDS):
                logger.debug(
                    "Device %s authenticated with previous key (rotated %ds ago)",
                    device.id, age.total_seconds(),
                )
                return  # previous key still within grace window

        raise HTTPException(status_code=401, detail="Invalid device API key")

    # 2. Fall back to browser session cookie
    from cms.auth import COOKIE_NAME, _resolve_user_from_session
    cookie = request.cookies.get(COOKIE_NAME)
    if cookie:
        user = await _resolve_user_from_session(cookie, settings, db)
        if user is not None:
            return  # valid browser session

    raise HTTPException(
        status_code=401,
        detail="Authentication required. Provide X-Device-API-Key header or valid session.",
    )


# Separate router for device-facing download endpoints (device key auth)
device_router = APIRouter(
    prefix="/api/assets",
    dependencies=[Depends(require_device_or_session_auth)],
)

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
    - the user's groups include the asset via GroupAsset, OR
    - the user uploaded it AND it has no group assignments (personal/unshared)
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

    # Assets the user uploaded that have NO group assignments (personal/unshared)
    own_q = (
        select(Asset.id)
        .where(Asset.uploaded_by_user_id == user.id)
        .where(~Asset.id.in_(select(GroupAsset.asset_id)))
    )

    global_ids = set((await db.execute(global_q)).scalars().all())
    group_asset_ids = set((await db.execute(group_q)).scalars().all())
    own_ids = set((await db.execute(own_q)).scalars().all())

    return list(global_ids | group_asset_ids | own_ids)


async def _verify_asset_access(asset_id: uuid.UUID, request, db: AsyncSession) -> None:
    """Raise 403 if the current user cannot access this asset."""
    user = getattr(request.state, "user", None)
    if not user:
        return
    visible = await _visible_asset_ids(user, db)
    if visible is not None and asset_id not in visible:
        raise HTTPException(status_code=403, detail="Not authorised for this asset")


@router.get("/status", dependencies=[Depends(require_permission(ASSETS_READ))])
async def assets_status_json(
    user: User = Depends(require_permission(ASSETS_READ)),
    db: AsyncSession = Depends(get_db),
):
    """Lightweight JSON for assets page polling — filtered by user's group access."""
    from sqlalchemy import func as sa_func

    visible = await _visible_asset_ids(user, db)
    user_group_ids = await get_user_group_ids(user, db)

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

    # Build group name map for scope data
    groups_result = await db.execute(select(DeviceGroup.id, DeviceGroup.name))
    group_name_map = {str(r[0]): r[1] for r in groups_result.all()}

    result = await db.execute(
        asset_q
        .options(
            selectinload(Asset.variants).selectinload(AssetVariant.profile),
            selectinload(Asset.group_asset_links),
        )
        .order_by(Asset.uploaded_at.desc())
    )
    assets_detail = []
    for a in result.scalars().all():
        variants = []
        a_ready = a_processing = a_failed = 0
        for v in sorted(a.variants, key=lambda v: (v.profile.name if v.profile else "")):
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
            "is_global": a.is_global,
            "has_uploader": a.uploaded_by_user_id is not None,
            "scope_groups": [
                {"id": str(ga.group_id), "name": group_name_map.get(str(ga.group_id), "?")}
                for ga in a.group_asset_links
                if user_group_ids is None or ga.group_id in user_group_ids
            ],
        })

    # Compute a hash of group-asset assignments so the poller can detect scope changes
    import hashlib
    ga_q = select(GroupAsset.asset_id, GroupAsset.group_id).order_by(GroupAsset.asset_id, GroupAsset.group_id)
    if visible is not None:
        ga_q = ga_q.where(GroupAsset.asset_id.in_(visible))
    ga_rows = (await db.execute(ga_q)).all()
    scope_hash = hashlib.md5(
        ",".join(f"{r[0]}:{r[1]}" for r in ga_rows).encode()
    ).hexdigest()[:12]

    return {
        "asset_count": asset_count,
        "variant_ready": variant_ready,
        "variant_processing": variant_processing,
        "variant_failed": variant_failed,
        "scope_hash": scope_hash,
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
    group_id: str | None = Query(None, description="Group UUID (single, for backward compat)"),
    group_ids: str | None = Query(None, description="Comma-separated group UUIDs"),
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

    # Resolve group UUIDs (support both single group_id and multi group_ids)
    resolved_groups: list[uuid.UUID] = []
    user_groups = await get_user_group_ids(user, db)
    is_admin = user_groups is None
    raw_ids = []
    if group_ids:
        raw_ids = [g.strip() for g in group_ids.split(",") if g.strip()]
    elif group_id:
        raw_ids = [group_id]
    for gid in raw_ids:
        try:
            parsed = uuid.UUID(gid)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid group_id: {gid}")
        if not is_admin and parsed not in user_groups:
            raise HTTPException(status_code=403, detail="You are not a member of this group")
        resolved_groups.append(parsed)

    # Only admin uploads without groups become global; others are personal
    make_global = (not resolved_groups and is_admin)

    # Database record
    asset = Asset(
        filename=final_filename,
        original_filename=original_filename,
        asset_type=asset_type,
        size_bytes=len(content),
        checksum=checksum,
        is_global=make_global,
        uploaded_by_user_id=user.id,
    )
    db.add(asset)
    await db.flush()

    # Create GroupAsset entries for all selected groups
    for gid in resolved_groups:
        db.add(GroupAsset(asset_id=asset.id, group_id=gid))

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
    await db.refresh(asset)

    # Queue transcoding for all profiles (video and image assets)
    if asset_type in (AssetType.VIDEO, AssetType.IMAGE):
        await _enqueue_transcoding(asset, db)
        from cms.services.transcoder import notify_worker
        await notify_worker(db)

    return asset


@router.post("/webpage", response_model=AssetOut, status_code=201)
async def create_webpage_asset(
    request: Request,
    user: User = Depends(require_permission(ASSETS_WRITE)),
    db: AsyncSession = Depends(get_db),
):
    """Create a webpage asset from a URL (no file upload)."""
    body = await request.json()
    url = body.get("url", "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="URL is required")
    if not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="URL must start with http:// or https://")
    if len(url) > 2048:
        raise HTTPException(status_code=400, detail="URL too long (max 2048 characters)")

    # Validate URL structure
    from urllib.parse import urlparse
    parsed = urlparse(url)
    if not parsed.netloc or "." not in parsed.netloc:
        raise HTTPException(status_code=400, detail="URL must contain a valid hostname (e.g. example.com)")
    # Block dangerous schemes that could slip through URL encoding tricks
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="Only http and https URLs are allowed")
    # Block loopback/internal addresses — these would resolve on the Pi
    # device and could expose local services (SSRF risk)
    hostname = parsed.hostname or ""
    _blocked = ("localhost", "127.0.0.1", "::1", "0.0.0.0")
    if hostname in _blocked or hostname.endswith(".local"):
        raise HTTPException(status_code=400, detail="URLs pointing to localhost or loopback addresses are not allowed")

    # Use provided name or derive from URL
    name = body.get("name", "").strip()
    if not name:
        name = parsed.netloc + (parsed.path if parsed.path != "/" else "")
        if len(name) > 200:
            name = name[:200]

    # Check for duplicate filename
    existing = await db.execute(select(Asset).where(Asset.filename == name))
    if existing.scalar_one_or_none():
        # Append a short hash to make unique
        import hashlib as _hl
        suffix = _hl.md5(url.encode()).hexdigest()[:6]
        name = f"{name} ({suffix})"

    # Resolve group UUIDs
    resolved_groups: list[uuid.UUID] = []
    user_groups = await get_user_group_ids(user, db)
    is_admin = user_groups is None
    raw_ids = body.get("group_ids", [])
    if isinstance(raw_ids, str):
        raw_ids = [g.strip() for g in raw_ids.split(",") if g.strip()]
    group_id = body.get("group_id")
    if group_id and not raw_ids:
        raw_ids = [group_id]
    for gid in raw_ids:
        try:
            parsed_id = uuid.UUID(str(gid))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid group_id: {gid}")
        if not is_admin and parsed_id not in user_groups:
            raise HTTPException(status_code=403, detail="You are not a member of this group")
        resolved_groups.append(parsed_id)

    make_global = (not resolved_groups and is_admin)

    asset = Asset(
        filename=name,
        asset_type=AssetType.WEBPAGE,
        size_bytes=0,
        checksum="",
        url=url,
        is_global=make_global,
        uploaded_by_user_id=user.id,
    )
    db.add(asset)
    await db.flush()

    for gid in resolved_groups:
        db.add(GroupAsset(asset_id=asset.id, group_id=gid))

    await db.commit()
    await db.refresh(asset)
    return asset


@router.post("/stream", response_model=AssetOut, status_code=201)
async def create_stream_asset(
    request: Request,
    user: User = Depends(require_permission(ASSETS_WRITE)),
    db: AsyncSession = Depends(get_db),
):
    """Create a stream asset from a video stream URL (HLS, DASH, RTMP, etc.)."""
    body = await request.json()
    url = body.get("url", "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="Stream URL is required")

    # Stream URLs support more schemes than webpages
    _allowed_schemes = ("http", "https", "rtmp", "rtmps", "rtsp", "rtsps", "mms", "mmsh")
    if not any(url.startswith(s + "://") for s in _allowed_schemes):
        raise HTTPException(
            status_code=400,
            detail=f"URL must start with one of: {', '.join(s + '://' for s in _allowed_schemes)}",
        )
    if len(url) > 2048:
        raise HTTPException(status_code=400, detail="URL too long (max 2048 characters)")

    from urllib.parse import urlparse
    parsed = urlparse(url)
    if not parsed.netloc:
        raise HTTPException(status_code=400, detail="URL must contain a valid hostname")
    # Block loopback/internal addresses (SSRF risk)
    hostname = parsed.hostname or ""
    _blocked = ("localhost", "127.0.0.1", "::1", "0.0.0.0")
    if hostname in _blocked or hostname.endswith(".local"):
        raise HTTPException(status_code=400, detail="URLs pointing to localhost or loopback addresses are not allowed")

    # Check for duplicate stream URL (per type)
    save_locally = body.get("save_locally", False)
    target_type = AssetType.SAVED_STREAM if save_locally else AssetType.STREAM

    # Capture duration for live streams being saved
    capture_duration = body.get("capture_duration")
    if capture_duration is not None:
        try:
            capture_duration = int(capture_duration)
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="capture_duration must be an integer (seconds)")
        if capture_duration < 10:
            raise HTTPException(status_code=400, detail="Capture duration must be at least 10 seconds")
        max_allowed = 14400  # 4 hours
        if capture_duration > max_allowed:
            raise HTTPException(status_code=400, detail=f"Capture duration cannot exceed {max_allowed} seconds (4 hours)")

    dup_q = await db.execute(
        select(Asset).where(
            Asset.url == url,
            Asset.asset_type == target_type,
        ).limit(1)
    )
    if dup_q.scalar_one_or_none():
        mode = "saved stream" if save_locally else "live stream"
        raise HTTPException(
            status_code=409,
            detail=f"A {mode} asset with this URL already exists",
        )

    # Use provided name or derive from URL
    name = body.get("name", "").strip()
    if not name:
        name = parsed.netloc + (parsed.path if parsed.path != "/" else "")
        if len(name) > 200:
            name = name[:200]

    # Check for duplicate filename
    existing = await db.execute(select(Asset).where(Asset.filename == name))
    if existing.scalar_one_or_none():
        import hashlib as _hl
        suffix = _hl.md5(url.encode()).hexdigest()[:6]
        name = f"{name} ({suffix})"

    # Resolve group UUIDs
    resolved_groups: list[uuid.UUID] = []
    user_groups = await get_user_group_ids(user, db)
    is_admin = user_groups is None
    raw_ids = body.get("group_ids", [])
    if isinstance(raw_ids, str):
        raw_ids = [g.strip() for g in raw_ids.split(",") if g.strip()]
    group_id = body.get("group_id")
    if group_id and not raw_ids:
        raw_ids = [group_id]
    for gid in raw_ids:
        try:
            parsed_id = uuid.UUID(str(gid))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid group_id: {gid}")
        if not is_admin and parsed_id not in user_groups:
            raise HTTPException(status_code=403, detail="You are not a member of this group")
        resolved_groups.append(parsed_id)

    make_global = (not resolved_groups and is_admin)

    asset = Asset(
        filename=name,
        asset_type=target_type,
        size_bytes=0,
        checksum="",
        url=url,
        is_global=make_global,
        uploaded_by_user_id=user.id,
        capture_duration=capture_duration if target_type == AssetType.SAVED_STREAM else None,
    )
    db.add(asset)
    await db.flush()

    for gid in resolved_groups:
        db.add(GroupAsset(asset_id=asset.id, group_id=gid))

    # If save_locally is enabled, enqueue transcoding so the worker
    # will capture the stream and create variants for each device profile
    if target_type == AssetType.SAVED_STREAM:
        await _enqueue_transcoding(asset, db)
        from cms.services.transcoder import notify_worker
        await notify_worker(db)

    await db.commit()
    await db.refresh(asset)
    return asset


@router.post("/{asset_id}/recapture")
async def recapture_stream(
    asset_id: uuid.UUID,
    user: User = Depends(require_permission(ASSETS_WRITE)),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """Re-capture a SAVED_STREAM asset: re-downloads the stream, overwrites
    the capture file, and resets all variants to PENDING for retranscoding."""
    result = await db.execute(select(Asset).where(Asset.id == asset_id))
    asset = result.scalar_one_or_none()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")

    if asset.asset_type != AssetType.SAVED_STREAM:
        raise HTTPException(status_code=400, detail="Only saved-stream assets can be re-captured")

    if not asset.url:
        raise HTTPException(status_code=400, detail="Asset has no stream URL")

    # Delete existing capture file
    storage = get_storage()
    capture_path = settings.asset_storage_path / asset.filename
    if capture_path.is_file():
        capture_path.unlink()
    await storage.on_file_deleted(asset.filename)

    # Reset filename to display name so worker re-captures
    asset.filename = asset.original_filename or f"{asset.id}_capture.mp4"
    asset.original_filename = None
    asset.checksum = ""
    asset.size_bytes = 0

    # Delete variant files and reset to PENDING
    from cms.services.transcoder import cancel_asset_transcodes
    cancel_asset_transcodes(asset_id)

    variants_dir = settings.asset_storage_path / "variants"
    var_result = await db.execute(
        select(AssetVariant).where(AssetVariant.source_asset_id == asset_id)
    )
    for variant in var_result.scalars().all():
        vpath = variants_dir / variant.filename
        if vpath.is_file():
            vpath.unlink()
        await storage.on_file_deleted(f"variants/{variant.filename}")
        variant.status = VariantStatus.PENDING
        variant.progress = 0.0
        variant.retry_count = 0
        variant.checksum = ""
        variant.size_bytes = 0

    await db.commit()

    # Notify worker to pick up the pending variants
    from cms.services.transcoder import notify_worker
    await notify_worker(db)

    return {"recaptured": True, "asset_id": str(asset_id)}


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
async def get_asset(asset_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Asset).where(Asset.id == asset_id))
    asset = result.scalar_one_or_none()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")
    await _verify_asset_access(asset_id, request, db)
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
    request: Request,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    await _verify_asset_access(asset_id, request, db)
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


@router.get("/variants/{variant_id}/preview", dependencies=[Depends(require_permission(ASSETS_READ))])
async def preview_variant(
    variant_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    result = await db.execute(select(AssetVariant).where(AssetVariant.id == variant_id))
    variant = result.scalar_one_or_none()
    if not variant:
        raise HTTPException(status_code=404, detail="Variant not found")

    file_path = settings.asset_storage_path / "variants" / variant.filename
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="Variant file not found on disk")

    ext = "." + variant.filename.rsplit(".", 1)[-1].lower()
    media_type = MIME_TYPES.get(ext, "application/octet-stream")

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

    # Only the owner or an admin can delete an asset
    user_groups = await get_user_group_ids(user, db)
    is_admin = user_groups is None
    if not is_admin and asset.uploaded_by_user_id != user.id:
        raise HTTPException(status_code=403, detail="Only the asset owner can delete this asset")

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
    request: Request,
    group_id: uuid.UUID = Query(..., description="Group UUID to share with"),
    user: User = Depends(require_permission(ASSETS_WRITE)),
    db: AsyncSession = Depends(get_db),
):
    """Share an asset with an additional group."""
    asset = (await db.execute(select(Asset).where(Asset.id == asset_id))).scalar_one_or_none()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")
    await _verify_asset_access(asset_id, request, db)

    # Check target group exists
    group = (await db.execute(select(DeviceGroup).where(DeviceGroup.id == group_id))).scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    # Scoped users can only share to groups they belong to
    user_groups = await get_user_group_ids(user, db)
    if user_groups is not None and group_id not in user_groups:
        raise HTTPException(status_code=403, detail="Cannot share to a group you are not a member of")

    # Check if already shared
    existing = (await db.execute(
        select(GroupAsset).where(GroupAsset.asset_id == asset_id, GroupAsset.group_id == group_id)
    )).scalar_one_or_none()
    if existing:
        return {"status": "already_shared"}

    db.add(GroupAsset(asset_id=asset_id, group_id=group_id))
    await db.commit()
    return {"status": "shared", "asset_id": str(asset_id), "group_id": str(group_id)}


@router.delete("/{asset_id}/share", dependencies=[Depends(require_permission(ASSETS_WRITE))])
async def unshare_asset(
    asset_id: uuid.UUID,
    request: Request,
    group_id: uuid.UUID = Query(..., description="Group UUID to unshare from"),
    user: User = Depends(require_permission(ASSETS_WRITE)),
    db: AsyncSession = Depends(get_db),
):
    """Remove an asset from a group."""
    await _verify_asset_access(asset_id, request, db)

    # Scoped users can only unshare from groups they belong to
    user_groups = await get_user_group_ids(user, db)
    if user_groups is not None and group_id not in user_groups:
        raise HTTPException(status_code=403, detail="Cannot unshare from a group you are not a member of")

    ga = (await db.execute(
        select(GroupAsset).where(GroupAsset.asset_id == asset_id, GroupAsset.group_id == group_id)
    )).scalar_one_or_none()
    if not ga:
        raise HTTPException(status_code=404, detail="Asset is not shared with this group")
    await db.delete(ga)
    await db.commit()

    # Check if asset is still visible to the requesting user after unshare
    visible = await _visible_asset_ids(user, db)
    still_visible = visible is None or asset_id in visible

    return {"status": "unshared", "asset_id": str(asset_id), "group_id": str(group_id),
            "still_visible": still_visible}


@router.post("/{asset_id}/global", dependencies=[Depends(require_permission(ASSETS_WRITE))])
async def toggle_asset_global(
    asset_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Toggle an asset's global visibility."""
    await _verify_asset_access(asset_id, request, db)
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
