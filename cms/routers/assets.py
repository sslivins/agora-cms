"""Asset library API routes with RBAC group scoping."""

import hashlib
import logging
import re
import uuid
from pathlib import Path
from typing import List

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, selectinload

from cms.auth import get_settings, get_user_group_ids, require_auth, require_permission
from cms.config import Settings
from cms.database import get_db
from cms.permissions import ASSETS_READ, ASSETS_WRITE
from cms.models.asset import Asset, AssetType, AssetVariant, DeviceAsset, VariantStatus
from cms.models.device import Device, DeviceGroup
from cms.models.device_profile import DeviceProfile
from cms.models.group_asset import GroupAsset
from cms.models.schedule import Schedule
from cms.models.slideshow_slide import SlideshowSlide
from cms.models.tag import AssetTag, Tag
from cms.models.user import User
from cms.models.chat_thread import ChatThread
from cms.schemas.asset import (
    AssetBulkFailure,
    AssetBulkIn,
    AssetBulkOut,
    AssetOut,
    AssetPageOut,
    AssetUsage,
    AssetUsageRef,
    BULK_ACTIONS,
    MAX_SLIDE_DURATION_MS,
    MAX_SLIDESHOW_SLIDES,
    SlideIn,
)
from cms.schemas.tag import TagOut
from cms.services.audit_service import audit_log, compute_diff
from cms.services.asset_readiness import composed_unpublished_reason
from cms.services.assistant.mcp_client import MODE_SLIDESHOW_EDITOR
from cms.services.assistant_flag import assistant_enabled_for
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


async def _unique_filename(db: AsyncSession, desired: str, *, max_attempts: int = 1000) -> str:
    """Return ``desired`` if no asset has that filename, otherwise append
    ``_1``, ``_2``, ... to the stem until a free name is found.

    Example: if ``promo.mp4`` exists, returns ``promo_1.mp4``. If that also
    exists, returns ``promo_2.mp4``, etc. Extension is preserved; files
    without an extension get a trailing ``_N``.

    Considers ALL assets including soft-deleted ones, because the
    ``filename`` column carries a DB-level unique constraint — a
    soft-deleted row still reserves its name. Monotonic suffixes also give
    predictable, human-friendly names (no reuse of gaps).
    """
    p = Path(desired)
    stem = p.stem or desired
    suffix = p.suffix  # includes the leading dot, or '' if no extension

    candidate = desired
    for n in range(0, max_attempts):
        if n > 0:
            candidate = f"{stem}_{n}{suffix}"
        existing = await db.execute(
            select(Asset.id).where(Asset.filename == candidate)
        )
        if existing.scalar_one_or_none() is None:
            return candidate
    # Extremely unlikely — 1000 identically-named assets. Fall through with
    # a UUID suffix so we never raise from here.
    return f"{stem}_{uuid.uuid4().hex[:8]}{suffix}"


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
    asset_q = select(Asset).where(Asset.deleted_at.is_(None))
    if visible is not None:
        asset_q = asset_q.where(Asset.id.in_(visible))

    asset_count = (await db.execute(
        select(sa_func.count(Asset.id)).where(Asset.id.in_(visible)) if visible is not None
        else select(sa_func.count(Asset.id))
    )).scalar() or 0

    variant_ready = 0
    variant_processing = 0
    variant_failed = 0

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
    all_assets = result.scalars().all()

    # Slide counts for slideshow assets so the page poller can re-render the
    # "N slides" badge without it flipping back to "none" (the JS poller
    # would otherwise treat a slideshow with 0 variants as a generic asset).
    slide_counts: dict = {}
    slideshow_ids = [a.id for a in all_assets if a.asset_type == AssetType.SLIDESHOW]
    if slideshow_ids:
        sc_rows = (await db.execute(
            select(SlideshowSlide.slideshow_asset_id, sa_func.count())
            .where(SlideshowSlide.slideshow_asset_id.in_(slideshow_ids))
            .group_by(SlideshowSlide.slideshow_asset_id)
        )).all()
        slide_counts = {sid: cnt for sid, cnt in sc_rows}

    assets_detail = []
    thumb_map = await _thumbnail_urls_for([a.id for a in all_assets], db)
    for a in all_assets:
        # Collapse to newest live row per profile for consistency with
        # the Library template render (cms/ui.py assets_page).
        from cms.services.variant_view import collapse_to_latest
        # Drop CMS-internal variants (thumbnail profile) from the
        # device-facing readiness picture entirely — they don't gate
        # device delivery and their status would confuse the per-asset
        # progress badge.
        device_variants = [
            v for v in a.variants
            if v.profile is None
            or getattr(v.profile, "purpose", "device") == "device"
        ]
        visible_variants = collapse_to_latest(device_variants)
        variants = []
        a_ready = a_processing = a_failed = 0
        for v in sorted(visible_variants, key=lambda v: (v.profile.name if v.profile else "")):
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
        # Sum into top-level totals so the page-level badges count only
        # what the user actually sees (one row per (asset, profile) slot).
        variant_ready += a_ready
        variant_processing += a_processing
        variant_failed += a_failed
        # Schedule-picker readiness, computed identically to the server
        # render in cms/ui.py (issue #201) so pages that poll this endpoint
        # can self-heal a "transcoding…" disabled state once the variants
        # finish — without a full page reload. Mirror ui.py exactly: use the
        # full variant list (not the device-filtered view) and apply the
        # composed-unpublished override.
        from cms.services.variant_view import is_asset_ready as _is_asset_ready
        _ready, _reason = _is_asset_ready(a.variants)
        _composed_reason = composed_unpublished_reason(a)
        if _ready and _composed_reason:
            _ready, _reason = False, _composed_reason
        # Build group name map for scope data
        variant_progress_sum = sum((v.progress or 0.0) for v in visible_variants)
        aggregate_pct = round(variant_progress_sum / len(visible_variants), 1) if visible_variants else 0.0
        assets_detail.append({
            "id": str(a.id),
            "asset_type": a.asset_type.value,
            "variant_total": len(visible_variants),
            "variant_ready": a_ready,
            "variant_processing": a_processing,
            "variant_failed": a_failed,
            "variant_aggregate_progress": aggregate_pct,
            "capture_progress": a.capture_progress,
            "capture_error": a.capture_error,
            "variants": variants,
            "is_global": a.is_global,
            "has_uploader": a.uploaded_by_user_id is not None,
            "scope_groups": [
                {"id": str(ga.group_id), "name": group_name_map.get(str(ga.group_id), "?")}
                for ga in a.group_asset_links
                if user_group_ids is None or ga.group_id in user_group_ids
            ],
            "slide_count": slide_counts.get(a.id, 0) if a.asset_type == AssetType.SLIDESHOW else None,
            "thumbnail_url": thumb_map.get(a.id),
            # Mirror the main-list AssetOut.unpublished so the status poller's
            # buildVariantBadge() keeps the "Unpublished" badge instead of
            # overwriting it with "none" on the first reconcile.
            "unpublished": composed_unpublished_reason(a) is not None,
            # Schedule-picker readiness (issue #201) so the schedules page can
            # refresh a stale "transcoding…" disabled option live.
            "ready_for_selection": _ready,
            "not_ready_reason": _reason,
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
    q = select(Asset).where(Asset.deleted_at.is_(None)).order_by(Asset.uploaded_at.desc())
    if visible is not None:
        q = q.where(Asset.id.in_(visible))
    result = await db.execute(q)
    assets = list(result.scalars().all())
    thumbs = await _thumbnail_urls_for([a.id for a in assets], db)
    tags = await _tags_for([a.id for a in assets], db)
    usage = await _usage_for([a.id for a in assets], db)
    return [_asset_out_with_thumb(a, thumbs, tags, usage) for a in assets]


# ── Paginated / filtered listing for the asset library UI ──
#
# This sits alongside the legacy ``GET /api/assets`` (above) so the many
# existing callers (MCP client, slideshow_builder, half the test suite)
# keep working with the old flat-list shape. Only the new asset-library
# UI talks to ``/page``.


class _OrderSpec:
    """Maps an ``order=`` query value to (sort column, secondary cursor)."""

    def __init__(self, column, *, nulls_last: bool = False, coalesce_with=None):
        self.column = column
        self.nulls_last = nulls_last
        self.coalesce_with = coalesce_with

    def expr(self, descending: bool):
        from sqlalchemy import nulls_last as _nl

        col = self.column
        if self.coalesce_with is not None:
            col = func.coalesce(self.column, self.coalesce_with)
        ordered = col.desc() if descending else col.asc()
        if self.nulls_last:
            ordered = _nl(ordered)
        return ordered, col


def _build_order_specs() -> dict[str, _OrderSpec]:
    # Lazily built so the column references resolve after the model is
    # fully imported.
    return {
        "display_name": _OrderSpec(
            Asset.display_name, coalesce_with=Asset.filename
        ),
        "asset_type": _OrderSpec(Asset.asset_type),
        "size_bytes": _OrderSpec(Asset.size_bytes),
        "uploaded_at": _OrderSpec(Asset.uploaded_at),
        "duration_seconds": _OrderSpec(
            Asset.duration_seconds, nulls_last=True
        ),
    }


def _encode_cursor(value, last_id: uuid.UUID) -> str:
    import base64
    import json

    if isinstance(value, datetime):
        payload_value = value.isoformat()
    elif isinstance(value, uuid.UUID):
        payload_value = str(value)
    elif isinstance(value, AssetType):
        payload_value = value.value
    else:
        payload_value = value
    raw = json.dumps([payload_value, str(last_id)], separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple:
    import base64
    import json

    pad = "=" * ((4 - len(cursor) % 4) % 4)
    try:
        raw = base64.urlsafe_b64decode(cursor + pad).decode()
        value, last_id = json.loads(raw)
        return value, uuid.UUID(last_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid pagination cursor")


_ALLOWED_TYPES = {t.value for t in AssetType}


async def _thumbnail_urls_for(
    asset_ids: list[uuid.UUID], db: AsyncSession
) -> dict[uuid.UUID, str]:
    """Return a {asset_id -> thumbnail variant URL} map for the given
    ids, including only assets whose thumbnail variant is READY.

    A single SQL round-trip joins ``device_profiles`` so we filter on
    ``purpose='thumbnail'`` server-side. Assets with no ready thumbnail
    (pending, failed, deleted, or asset type that can't produce one)
    are simply absent from the result -- callers should treat that as
    ``thumbnail_url=None``.

    SLIDESHOW assets own no thumbnail variant of their own, so they fall
    back to the first slide's (lowest ``position``) source-asset
    thumbnail. This makes a slideshow render as the deck it represents
    everywhere ``thumbnail_url`` is consumed (table + grid views).
    """
    if not asset_ids:
        return {}

    out = await _direct_thumbnail_urls(asset_ids, db)

    # SLIDESHOW assets have no thumbnail variant of their own. Fall back
    # to the first slide's (lowest ``position``) source-asset thumbnail so
    # a slideshow reads visually as the deck it represents. Any requested
    # id that resolved directly above is left untouched.
    missing = [aid for aid in asset_ids if aid not in out]
    if missing:
        slide_rows = (await db.execute(
            select(
                SlideshowSlide.slideshow_asset_id,
                SlideshowSlide.source_asset_id,
                SlideshowSlide.position,
            )
            .where(SlideshowSlide.slideshow_asset_id.in_(missing))
            .order_by(
                SlideshowSlide.slideshow_asset_id,
                SlideshowSlide.position.asc(),
            )
        )).all()
        # First (lowest-position) source per slideshow.
        first_source: dict[uuid.UUID, uuid.UUID] = {}
        for ss_id, src_id, _pos in slide_rows:
            first_source.setdefault(ss_id, src_id)
        if first_source:
            # Resolve the source thumbnails directly (no recursion into
            # ``_thumbnail_urls_for`` -- a source is never itself a
            # slideshow, and the direct helper sidesteps any cycle risk).
            src_thumbs = await _direct_thumbnail_urls(
                list(set(first_source.values())), db
            )
            for ss_id, src_id in first_source.items():
                url = src_thumbs.get(src_id)
                if url is not None:
                    out[ss_id] = url
    return out


async def _direct_thumbnail_urls(
    asset_ids: list[uuid.UUID], db: AsyncSession
) -> dict[uuid.UUID, str]:
    """Return a {asset_id -> thumbnail variant URL} map for assets that
    own a READY ``purpose='thumbnail'`` variant directly.

    This is the raw variant lookup that backs :func:`_thumbnail_urls_for`;
    it does NOT apply the slideshow first-slide fallback.
    """
    if not asset_ids:
        return {}
    rows = (await db.execute(
        select(AssetVariant.source_asset_id, AssetVariant.id)
        .join(DeviceProfile, AssetVariant.profile_id == DeviceProfile.id)
        .where(
            AssetVariant.source_asset_id.in_(asset_ids),
            AssetVariant.status == VariantStatus.READY,
            AssetVariant.deleted_at.is_(None),
            DeviceProfile.purpose == "thumbnail",
        )
        .order_by(AssetVariant.created_at.desc())
    )).all()
    out: dict[uuid.UUID, str] = {}
    for aid, vid in rows:
        # First (newest) wins per asset.
        out.setdefault(aid, f"/api/assets/variants/{vid}/preview")
    return out


def _asset_out_with_thumb(
    asset: Asset, thumbs: dict[uuid.UUID, str],
    tags: dict[uuid.UUID, list[TagOut]] | None = None,
    usage: dict[uuid.UUID, AssetUsage] | None = None,
) -> AssetOut:
    out = AssetOut.model_validate(asset)
    out.unpublished = composed_unpublished_reason(asset) is not None
    if asset.id in thumbs:
        out.thumbnail_url = thumbs[asset.id]
    if tags is not None and asset.id in tags:
        out.tags = tags[asset.id]
    if usage is not None:
        out.usage = usage.get(asset.id) or AssetUsage()
    return out


async def _tags_for(
    asset_ids: list[uuid.UUID], db: AsyncSession
) -> dict[uuid.UUID, list[TagOut]]:
    """Return ``{asset_id: [TagOut, ...]}`` in a single round-trip.

    Avoids the N+1 trap that a naive ``Asset.tags`` access on each row
    would otherwise produce when serialising a 50-item page.  Tags are
    ordered by name for stable rendering.
    """
    if not asset_ids:
        return {}
    rows = (await db.execute(
        select(AssetTag.asset_id, Tag.id, Tag.name, Tag.color)
        .join(Tag, Tag.id == AssetTag.tag_id)
        .where(AssetTag.asset_id.in_(asset_ids))
        .order_by(Tag.name)
    )).all()
    out: dict[uuid.UUID, list[TagOut]] = {}
    for aid, tid, name, color in rows:
        out.setdefault(aid, []).append(TagOut(id=tid, name=name, color=color))
    return out


# Maximum number of named references included in each usage list.
# Anything past this lands in ``extra_*`` so the badge tooltip can render
# "and N more" without bloating list responses.
_USAGE_NAME_CAP = 10


async def _usage_for(
    asset_ids: list[uuid.UUID], db: AsyncSession
) -> dict[uuid.UUID, AssetUsage]:
    """Return ``{asset_id: AssetUsage}`` describing where each asset is
    referenced.

    "Referenced" means either:

    - any non-expired schedule row (``end_date IS NULL OR end_date >=
      NOW()``). ``enabled`` state is ignored on purpose: a disabled
      schedule still represents intent and should count as "in use".
    - any slideshow that uses this asset as a slide
      (``slideshow_slides.source_asset_id``).

    Two batched queries (no per-asset round-trips). Schedule and
    slideshow names are included up to ``_USAGE_NAME_CAP``; overflow is
    surfaced via ``extra_schedules`` / ``extra_slides`` so the UI can
    render "Schedule A, Schedule B, +3 more" without paying for every
    name in every payload.
    """
    if not asset_ids:
        return {}
    now_utc = datetime.now(timezone.utc)

    out: dict[uuid.UUID, AssetUsage] = {aid: AssetUsage() for aid in asset_ids}

    sched_rows = (await db.execute(
        select(Schedule.asset_id, Schedule.id, Schedule.name)
        .where(
            Schedule.asset_id.in_(asset_ids),
            (Schedule.end_date.is_(None)) | (Schedule.end_date >= now_utc),
        )
        .order_by(Schedule.asset_id, Schedule.name)
    )).all()
    sched_by_asset: dict[uuid.UUID, list[tuple[uuid.UUID, str]]] = {}
    for aid, sid, sname in sched_rows:
        sched_by_asset.setdefault(aid, []).append((sid, sname))

    slide_rows = (await db.execute(
        select(
            SlideshowSlide.source_asset_id,
            Asset.id,
            Asset.display_name,
            Asset.original_filename,
            Asset.filename,
        )
        .join(Asset, Asset.id == SlideshowSlide.slideshow_asset_id)
        .where(
            SlideshowSlide.source_asset_id.in_(asset_ids),
            Asset.deleted_at.is_(None),
        )
        .order_by(SlideshowSlide.source_asset_id, Asset.id)
    )).all()
    slides_by_asset: dict[uuid.UUID, list[tuple[uuid.UUID, str]]] = {}
    for source_id, ss_id, display_name, original_filename, filename in slide_rows:
        bucket = slides_by_asset.setdefault(source_id, [])
        if bucket and bucket[-1][0] == ss_id:
            # Same slideshow appears once per slide it contains; collapse
            # to one entry per (source, slideshow). The order_by above
            # guarantees duplicates are adjacent.
            continue
        label = display_name or original_filename or filename or str(ss_id)
        bucket.append((ss_id, label))

    for aid in asset_ids:
        scheds = sched_by_asset.get(aid, [])
        slides = slides_by_asset.get(aid, [])
        usage = out[aid]
        usage.schedules = [
            AssetUsageRef(id=sid, name=sname) for sid, sname in scheds[:_USAGE_NAME_CAP]
        ]
        usage.extra_schedules = max(0, len(scheds) - _USAGE_NAME_CAP)
        usage.slides = [
            AssetUsageRef(id=ss_id, name=lbl) for ss_id, lbl in slides[:_USAGE_NAME_CAP]
        ]
        usage.extra_slides = max(0, len(slides) - _USAGE_NAME_CAP)
        usage.total = len(scheds) + len(slides)
    return out


@router.get("/page", response_model=AssetPageOut)
async def list_assets_paged(
    user: User = Depends(require_permission(ASSETS_READ)),
    db: AsyncSession = Depends(get_db),
    q: str = Query("", description="Substring search across name/filename"),
    type: list[str] = Query(default_factory=list, alias="type"),
    group_id: list[uuid.UUID] = Query(default_factory=list, alias="group_id"),
    uploader_id: list[uuid.UUID] = Query(default_factory=list, alias="uploader_id"),
    tag_id: list[uuid.UUID] = Query(default_factory=list, alias="tag_id"),
    uploaded_after: datetime | None = Query(None),
    uploaded_before: datetime | None = Query(None),
    usage: str | None = Query(
        None,
        description=(
            "Filter by usage. 'used': asset is referenced by a non-expired "
            "schedule OR a slideshow slide. 'unused': neither."
        ),
    ),
    order: str = Query("-uploaded_at"),
    cursor: str | None = Query(None),
    page_size: int = Query(50, ge=1, le=200),
):
    """Paginated + filtered asset listing for the asset library UI.

    All filters are AND-composed. ``q`` is a case-insensitive substring
    match against ``display_name``, ``original_filename``, and
    ``filename`` (Postgres-side this hits a trigram GIN index; SQLite
    falls back to a sequential scan in tests). The caller's group ACL
    is applied last and cannot be widened by URL parameters.
    """
    # ── Parse + validate order ──
    descending = order.startswith("-")
    key = order.lstrip("-")
    specs = _build_order_specs()
    if key not in specs:
        raise HTTPException(
            status_code=400,
            detail=f"order must be one of {sorted(specs)} (optionally prefixed with -)",
        )
    spec = specs[key]
    order_expr, order_col = spec.expr(descending)

    # ── Validate type filter ──
    if type:
        bad = [t for t in type if t not in _ALLOWED_TYPES]
        if bad:
            raise HTTPException(
                status_code=400,
                detail=f"unknown type filter values: {bad}",
            )
        type_enums = [AssetType(t) for t in type]
    else:
        type_enums = []

    # ── Build the filter predicates once, apply to both the page query
    #    and the (separate) count query. The count MUST NOT include the
    #    cursor predicate, otherwise total_estimate shrinks on each
    #    successive page (e.g. "61 → 11" after one scroll) which makes
    #    the load-more footer report nonsense.
    filter_clauses = [Asset.deleted_at.is_(None)]

    # ── ACL ──
    visible = await _visible_asset_ids(user, db)
    if visible is not None:
        filter_clauses.append(Asset.id.in_(visible))

    # ── Filters ──
    if q:
        like = f"%{q}%"
        filter_clauses.append(
            func.coalesce(Asset.display_name, "").ilike(like)
            | func.coalesce(Asset.original_filename, "").ilike(like)
            | Asset.filename.ilike(like)
        )

    if type_enums:
        filter_clauses.append(Asset.asset_type.in_(type_enums))

    if uploader_id:
        filter_clauses.append(Asset.uploaded_by_user_id.in_(uploader_id))

    if uploaded_after is not None:
        filter_clauses.append(Asset.uploaded_at >= uploaded_after)
    if uploaded_before is not None:
        filter_clauses.append(Asset.uploaded_at < uploaded_before)

    if group_id:
        # AND-intersected with the ACL: caller is still bounded by what
        # they can see — non-admins passing a group they aren't a member
        # of just see an empty page rather than getting 403.
        filter_clauses.append(
            Asset.id.in_(
                select(GroupAsset.asset_id).where(GroupAsset.group_id.in_(group_id))
            )
        )

    if tag_id:
        # AND-semantics across multiple tags: asset must have ALL of the
        # selected tags.  Implemented as a grouped subquery so we don't
        # need a JOIN+HAVING on the outer select (which would force the
        # cursor logic to do a DISTINCT and break the deterministic
        # ordering).
        n_tags = len(set(tag_id))
        filter_clauses.append(
            Asset.id.in_(
                select(AssetTag.asset_id)
                .where(AssetTag.tag_id.in_(tag_id))
                .group_by(AssetTag.asset_id)
                .having(func.count(func.distinct(AssetTag.tag_id)) == n_tags)
            )
        )

    if usage is not None:
        if usage not in ("used", "unused"):
            raise HTTPException(
                status_code=400,
                detail="usage must be one of: used, unused",
            )
        now_utc = datetime.now(timezone.utc)
        # An asset is "used" when either a non-expired schedule references
        # it (disabled-but-not-expired counts) OR a slideshow slide
        # references it (and the parent slideshow isn't soft-deleted).
        non_expired_sched_subq = (
            select(Schedule.asset_id).where(
                (Schedule.end_date.is_(None)) | (Schedule.end_date >= now_utc),
            )
        )
        slideshow_alias = aliased(Asset)
        slide_used_subq = (
            select(SlideshowSlide.source_asset_id)
            .join(slideshow_alias, slideshow_alias.id == SlideshowSlide.slideshow_asset_id)
            .where(slideshow_alias.deleted_at.is_(None))
        )
        if usage == "used":
            filter_clauses.append(
                Asset.id.in_(non_expired_sched_subq)
                | Asset.id.in_(slide_used_subq)
            )
        else:  # unused
            filter_clauses.append(Asset.id.notin_(non_expired_sched_subq))
            filter_clauses.append(Asset.id.notin_(slide_used_subq))

    stmt = select(Asset).where(*filter_clauses)

    # ── Cursor (encodes the order key value and the last id seen) ──
    if cursor:
        cursor_value, cursor_last_id = _decode_cursor(cursor)
        # Re-hydrate the cursor value into the order column's native type
        # so the WHERE clause type-checks on Postgres.
        if key == "uploaded_at":
            try:
                cursor_value = datetime.fromisoformat(cursor_value)
            except Exception:
                raise HTTPException(status_code=400, detail="Invalid cursor value")
        elif key == "asset_type":
            try:
                cursor_value = AssetType(cursor_value)
            except Exception:
                raise HTTPException(status_code=400, detail="Invalid cursor value")
        # size_bytes / duration_seconds come back as int / float from JSON
        # already; display_name comes back as a string. Nothing to do.

        if descending:
            stmt = stmt.where(
                (order_col < cursor_value)
                | ((order_col == cursor_value) & (Asset.id < cursor_last_id))
            )
        else:
            stmt = stmt.where(
                (order_col > cursor_value)
                | ((order_col == cursor_value) & (Asset.id > cursor_last_id))
            )

    # ── Order: primary key then id as deterministic tiebreaker ──
    id_tiebreak = Asset.id.desc() if descending else Asset.id.asc()
    stmt = stmt.order_by(order_expr, id_tiebreak).limit(page_size + 1)

    rows = (await db.execute(stmt)).all()
    has_more = len(rows) > page_size
    rows = rows[:page_size]

    items = [row[0] for row in rows]
    thumbs = await _thumbnail_urls_for([a.id for a in items], db)
    tags = await _tags_for([a.id for a in items], db)
    usage_map = await _usage_for([a.id for a in items], db)

    # ── Total estimate: count all rows matching the filters, ignoring
    #    the cursor. We only need this once per filter context but cost
    #    is low enough (single round-trip, hits the same indexes) to
    #    compute every page; UI uses it to render "Showing N of M".
    total_estimate = int(
        (await db.execute(
            select(func.count()).select_from(Asset).where(*filter_clauses)
        )).scalar() or 0
    )

    next_cursor: str | None = None
    if has_more and items:
        last = items[-1]
        # Pull the same expression value we ordered by (handles
        # coalesce(display_name, filename) etc.).
        last_value = getattr(last, key, None)
        if key == "display_name" and last_value is None:
            last_value = last.filename
        next_cursor = _encode_cursor(last_value, last.id)

    return AssetPageOut(
        items=[_asset_out_with_thumb(a, thumbs, tags, usage_map) for a in items],
        next_cursor=next_cursor,
        total_estimate=total_estimate,
    )


# ── Bulk endpoint ──
#
# Polymorphic. Routes per-id to the existing single-asset endpoint
# logic so all ACL invariants (schedule references, slideshow audience,
# group-membership scoping, admin-only global toggles) are enforced
# exactly as they would be in the per-asset path. Failures are reported
# per-id rather than failing the whole batch — same shape as ``POST
# /api/devices/bulk``.

@router.post(
    "/bulk",
    response_model=AssetBulkOut,
    dependencies=[Depends(require_permission(ASSETS_WRITE))],
)
async def bulk_assets(
    payload: AssetBulkIn,
    request: Request,
    user: User = Depends(require_permission(ASSETS_WRITE)),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    if payload.action in ("add_group", "remove_group") and payload.group_id is None:
        raise HTTPException(
            status_code=400,
            detail=f"group_id is required for action={payload.action!r}",
        )
    if payload.action in ("add_tag", "remove_tag"):
        if payload.tag_id is None:
            raise HTTPException(
                status_code=400,
                detail=f"tag_id is required for action={payload.action!r}",
            )
        # Resolve the tag once up-front -- a missing tag is a request
        # error (400), not a per-asset failure.
        tag_row = (
            await db.execute(select(Tag).where(Tag.id == payload.tag_id))
        ).scalar_one_or_none()
        if tag_row is None:
            raise HTTPException(status_code=404, detail="Tag not found")
    if payload.action == "set_global":
        if payload.is_global is None:
            raise HTTPException(
                status_code=400,
                detail="is_global is required for action='set_global'",
            )
        # set_global is admin-only — same posture as the single-asset
        # toggle endpoint, but expressed up-front for clarity.
        user_groups_check = await get_user_group_ids(user, db)
        if user_groups_check is not None:
            raise HTTPException(
                status_code=403,
                detail="Only admins can change an asset's global flag",
            )

    succeeded: list[uuid.UUID] = []
    failed: list[AssetBulkFailure] = []

    # De-duplicate while preserving order — callers occasionally send the
    # same id twice from a misbehaving UI; a duplicate would just produce
    # a 404 on the second pass which is noisy without being useful.
    seen: set[uuid.UUID] = set()
    asset_ids = [a for a in payload.asset_ids if not (a in seen or seen.add(a))]

    for aid in asset_ids:
        try:
            if payload.action == "delete":
                await delete_asset(
                    asset_id=aid, request=request, user=user, db=db, settings=settings
                )
            elif payload.action == "add_group":
                await share_asset(
                    asset_id=aid,
                    request=request,
                    group_id=payload.group_id,  # type: ignore[arg-type]
                    user=user,
                    db=db,
                )
            elif payload.action == "remove_group":
                await unshare_asset(
                    asset_id=aid,
                    request=request,
                    group_id=payload.group_id,  # type: ignore[arg-type]
                    user=user,
                    db=db,
                )
            elif payload.action == "set_global":
                # Toggle only if the current state differs from the
                # requested state so the operation is idempotent.
                cur = (
                    await db.execute(
                        select(Asset.is_global).where(
                            Asset.id == aid, Asset.deleted_at.is_(None)
                        )
                    )
                ).scalar_one_or_none()
                if cur is None:
                    raise HTTPException(status_code=404, detail="Asset not found")
                if bool(cur) != bool(payload.is_global):
                    await toggle_asset_global(
                        asset_id=aid, request=request, db=db
                    )
            elif payload.action == "add_tag":
                # Per-asset ACL check first (mirrors add_group posture).
                exists = (await db.execute(
                    select(Asset.id).where(Asset.id == aid, Asset.deleted_at.is_(None))
                )).scalar_one_or_none()
                if exists is None:
                    raise HTTPException(status_code=404, detail="Asset not found")
                await _verify_asset_access(aid, request, db)
                # Idempotent: skip if the (asset, tag) link already exists.
                existing = (await db.execute(
                    select(AssetTag.id).where(
                        AssetTag.asset_id == aid, AssetTag.tag_id == payload.tag_id
                    )
                )).scalar_one_or_none()
                if existing is None:
                    db.add(AssetTag(asset_id=aid, tag_id=payload.tag_id))
                    await audit_log(
                        db, user=user, action="asset.tag.add", resource_type="asset",
                        resource_id=str(aid),
                        description=f"Tagged asset with '{tag_row.name}'",
                        details={"tag_id": str(payload.tag_id), "tag_name": tag_row.name},
                        request=request,
                    )
                    await db.commit()
            elif payload.action == "remove_tag":
                exists = (await db.execute(
                    select(Asset.id).where(Asset.id == aid, Asset.deleted_at.is_(None))
                )).scalar_one_or_none()
                if exists is None:
                    raise HTTPException(status_code=404, detail="Asset not found")
                await _verify_asset_access(aid, request, db)
                deleted = (await db.execute(
                    delete(AssetTag).where(
                        AssetTag.asset_id == aid, AssetTag.tag_id == payload.tag_id
                    )
                )).rowcount
                if deleted:
                    await audit_log(
                        db, user=user, action="asset.tag.remove", resource_type="asset",
                        resource_id=str(aid),
                        description=f"Removed tag '{tag_row.name}' from asset",
                        details={"tag_id": str(payload.tag_id), "tag_name": tag_row.name},
                        request=request,
                    )
                    await db.commit()
            succeeded.append(aid)
        except HTTPException as exc:
            # Roll back any partial work the failed handler did so the
            # session is clean for the next iteration. The single-asset
            # endpoints commit on success, so rollback only affects the
            # current failed attempt.
            await db.rollback()
            failed.append(
                AssetBulkFailure(
                    id=aid,
                    reason=str(exc.detail),
                    status=exc.status_code,
                )
            )

    return AssetBulkOut(succeeded=succeeded, failed=failed)

@router.post("/upload", response_model=AssetOut, status_code=201)
async def upload_asset(
    file: UploadFile,
    request: Request,
    user: User = Depends(require_permission(ASSETS_WRITE)),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    group_id: str | None = Query(None, description="Group UUID (single, for backward compat)"),
    group_ids: str | None = Query(None, description="Comma-separated group UUIDs"),
):
    if not file.filename or not ALLOWED_PATTERN.match(file.filename):
        raise HTTPException(status_code=400, detail="Invalid filename")

    # Pick a unique stored filename. If file.filename is already taken by
    # a live asset, auto-rename (promo.mp4 -> promo_1.mp4) so different
    # users can upload files with the same name without colliding. The
    # original filename is preserved in ``original_filename`` for display.
    stored_filename = await _unique_filename(db, file.filename)
    was_renamed = stored_filename != file.filename

    # Read and hash
    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File too large")
    checksum = hashlib.sha256(content).hexdigest()

    # Store source file
    storage_dir = settings.asset_storage_path
    storage_dir.mkdir(parents=True, exist_ok=True)
    dest = storage_dir / stored_filename
    dest.write_bytes(content)

    asset_type = _asset_type(stored_filename)

    # Convert unsupported image formats to JPEG for device compatibility
    ext = "." + stored_filename.rsplit(".", 1)[-1].lower()
    final_filename = stored_filename
    original_filename = file.filename if was_renamed else None
    storage = get_storage()
    if asset_type == AssetType.IMAGE and ext in IMAGE_CONVERT_EXTS:
        from cms.services.transcoder import convert_image_to_jpeg
        # Pick a unique JPEG name — auto-rename if the .jpg name collides
        # with an existing live asset.
        jpeg_filename = await _unique_filename(
            db, Path(stored_filename).stem + ".jpg"
        )
        jpeg_path = storage_dir / jpeg_filename
        ok = await convert_image_to_jpeg(dest, jpeg_path)
        if not ok:
            dest.unlink(missing_ok=True)
            raise HTTPException(status_code=422, detail="Image conversion failed")
        # Keep original in originals/ for future re-transcoding
        originals_dir = storage_dir / "originals"
        originals_dir.mkdir(parents=True, exist_ok=True)
        dest.rename(originals_dir / stored_filename)
        # Preserve the user-supplied name (the HEIC filename) for display
        original_filename = file.filename
        content = jpeg_path.read_bytes()
        checksum = hashlib.sha256(content).hexdigest()
        final_filename = jpeg_filename
        # Sync converted JPEG + original to cloud storage
        await storage.on_file_stored(final_filename)
        await storage.on_file_stored(f"originals/{stored_filename}")
    else:
        # Sync source file to cloud storage
        await storage.on_file_stored(stored_filename)

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
        from cms.services.transcoder import (
            _enqueue_transcoding_for_asset,
            enqueue_variants,
        )
        variant_ids = await _enqueue_transcoding_for_asset(asset, db)
        await enqueue_variants(db, variant_ids)

    await audit_log(
        db, user=user, action="asset.upload", resource_type="asset",
        resource_id=str(asset.id),
        description=f"Uploaded {asset_type.value} asset '{final_filename}'",
        details={
            "filename": final_filename,
            "asset_type": asset_type.value,
            "size_bytes": len(content),
            "group_ids": [str(g) for g in resolved_groups],
            "is_global": make_global,
        },
        request=request,
    )
    await db.commit()

    return asset


def _validate_webpage_url(raw: str) -> tuple[str, "urllib.parse.ParseResult"]:
    """Validate + normalize a webpage URL. Raises HTTPException(400) on bad input.

    Returns the stripped URL and the parsed result. Shared by create_webpage_asset
    and update_asset (PATCH) so the two paths can't drift.
    """
    from urllib.parse import urlparse

    url = (raw or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="URL is required")
    if not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="URL must start with http:// or https://")
    if len(url) > 2048:
        raise HTTPException(status_code=400, detail="URL too long (max 2048 characters)")

    parsed = urlparse(url)
    if not parsed.netloc or "." not in parsed.netloc:
        raise HTTPException(status_code=400, detail="URL must contain a valid hostname (e.g. example.com)")
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="Only http and https URLs are allowed")
    hostname = parsed.hostname or ""
    _blocked = ("localhost", "127.0.0.1", "::1", "0.0.0.0")
    if hostname in _blocked or hostname.endswith(".local"):
        raise HTTPException(status_code=400, detail="URLs pointing to localhost or loopback addresses are not allowed")
    return url, parsed


@router.post("/webpage", response_model=AssetOut, status_code=201)
async def create_webpage_asset(
    request: Request,
    user: User = Depends(require_permission(ASSETS_WRITE)),
    db: AsyncSession = Depends(get_db),
):
    """Create a webpage asset from a URL (no file upload)."""
    body = await request.json()
    url, parsed = _validate_webpage_url(body.get("url", ""))

    # Use provided name or derive from URL
    name = body.get("name", "").strip()
    if not name:
        name = parsed.netloc + (parsed.path if parsed.path != "/" else "")
        if len(name) > 200:
            name = name[:200]

    # Check for duplicate filename (excluding soft-deleted)
    existing = await db.execute(
        select(Asset).where(Asset.filename == name, Asset.deleted_at.is_(None))
    )
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

    await audit_log(
        db, user=user, action="asset.create_webpage", resource_type="asset",
        resource_id=str(asset.id),
        description=f"Created webpage asset '{name}' ({url})",
        details={"filename": name, "url": url, "group_ids": [str(g) for g in resolved_groups],
                 "is_global": make_global},
        request=request,
    )
    await db.commit()
    await db.refresh(asset)

    # Best-effort: kick off a thumbnail snapshot of the live page. Never let
    # a thumbnail failure block asset creation.
    try:
        from cms.services.transcoder import enqueue_thumbnail
        await enqueue_thumbnail(asset, db)
    except Exception:  # noqa: BLE001
        logger.warning("Failed to enqueue webpage thumbnail for %s", asset.id, exc_info=True)

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
            Asset.deleted_at.is_(None),
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

    # Check for duplicate filename (excluding soft-deleted)
    existing = await db.execute(
        select(Asset).where(Asset.filename == name, Asset.deleted_at.is_(None))
    )
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

    # SAVED_STREAM: only notify the worker to capture the stream.
    # Variant creation happens later — the CMS monitor loop detects the
    # completed capture and enqueues transcoding, identical to the upload flow.

    await audit_log(
        db, user=user, action="asset.create_stream", resource_type="asset",
        resource_id=str(asset.id),
        description=f"Created {target_type.value} asset '{name}' ({url})",
        details={
            "filename": name,
            "url": url,
            "asset_type": target_type.value,
            "save_locally": save_locally,
            "capture_duration": capture_duration,
            "group_ids": [str(g) for g in resolved_groups],
            "is_global": make_global,
        },
        request=request,
    )
    await db.commit()
    await db.refresh(asset)

    if target_type == AssetType.SAVED_STREAM:
        from cms.services.transcoder import enqueue_stream_capture
        await enqueue_stream_capture(db, asset.id)
        await db.refresh(asset)

    return asset


# ── Slideshow assets ──


async def _load_and_validate_slide_sources(
    slides: list[SlideIn],
    db: AsyncSession,
    *,
    visible_ids: list[uuid.UUID] | None,
) -> tuple[dict[uuid.UUID, Asset], dict[uuid.UUID, set[uuid.UUID]]]:
    """Validate the slide list against the source assets it references.

    Returns ``(sources_by_id, source_groups)`` where ``sources_by_id`` maps
    each source asset id to its loaded ``Asset`` row, and ``source_groups``
    maps each source id to the set of group ids it has been shared with.
    Raises HTTPException for any validation failure.
    """
    if not slides:
        raise HTTPException(status_code=400, detail="Slideshow must have at least one slide")
    if len(slides) > MAX_SLIDESHOW_SLIDES:
        raise HTTPException(
            status_code=400,
            detail=f"Slideshow exceeds {MAX_SLIDESHOW_SLIDES} slide cap",
        )

    source_ids = list({s.source_asset_id for s in slides})
    rows = (
        await db.execute(
            select(Asset).where(Asset.id.in_(source_ids), Asset.deleted_at.is_(None))
        )
    ).scalars().all()
    sources_by_id: dict[uuid.UUID, Asset] = {a.id: a for a in rows}

    missing = [str(s.source_asset_id) for s in slides if s.source_asset_id not in sources_by_id]
    if missing:
        raise HTTPException(
            status_code=404,
            detail=f"Source asset(s) not found: {', '.join(sorted(set(missing)))}",
        )

    for s in slides:
        src = sources_by_id[s.source_asset_id]
        if src.asset_type not in (AssetType.IMAGE, AssetType.VIDEO, AssetType.COMPOSED):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Slide source '{src.filename}' has type "
                    f"{src.asset_type.value}; only image, video and composed "
                    "slides are allowed"
                ),
            )
        # A composed slide member must already be published — an
        # unpublished composed slide has no rendered bundle for the device
        # to download (it would 404 in a retry loop). Mirror the same gate
        # the scheduler/splash path applies via composed_unpublished_reason.
        if src.asset_type == AssetType.COMPOSED and composed_unpublished_reason(src):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Slide source '{src.filename}' is a composed slide that "
                    "hasn't been published yet. Open it in the editor and click "
                    "Publish before adding it to a slideshow."
                ),
            )
        if s.play_to_end and src.asset_type != AssetType.VIDEO:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Slide source '{src.filename}' is a {src.asset_type.value}; "
                    "play_to_end is only valid for video sources"
                ),
            )

    if visible_ids is not None:
        visible_set = set(visible_ids)
        unseen = sorted({
            str(s.source_asset_id)
            for s in slides
            if s.source_asset_id not in visible_set
        })
        if unseen:
            raise HTTPException(
                status_code=403,
                detail=f"Not authorised for source asset(s): {', '.join(unseen)}",
            )

    ga_rows = (
        await db.execute(
            select(GroupAsset.asset_id, GroupAsset.group_id).where(
                GroupAsset.asset_id.in_(source_ids)
            )
        )
    ).all()
    source_groups: dict[uuid.UUID, set[uuid.UUID]] = {sid: set() for sid in source_ids}
    for asset_id, group_id in ga_rows:
        source_groups[asset_id].add(group_id)

    return sources_by_id, source_groups


def _validate_slideshow_acl(
    slideshow_groups: set[uuid.UUID],
    slideshow_global: bool,
    sources_by_id: dict[uuid.UUID, Asset],
    source_groups: dict[uuid.UUID, set[uuid.UUID]],
) -> None:
    """Enforce that the slideshow's audience is a subset of every source's audience.

    A user who can see the slideshow can effectively reach each referenced
    source through it, so the source must already be visible to that user.
    Sufficient (slightly conservative) rule:

    * Global slideshow → every source must also be global.
    * Group-scoped slideshow → every source must be global, or shared with
      a superset of the slideshow's group set.
    * Personal/unshared slideshow (no groups, not global) → no extra check;
      ``_load_and_validate_slide_sources`` already required the uploader to
      have visibility on every source.
    """
    if slideshow_global:
        not_global = sorted(s.filename for s in sources_by_id.values() if not s.is_global)
        if not_global:
            raise HTTPException(
                status_code=400,
                detail=(
                    "A global slideshow can only reference global source assets. "
                    f"Not global: {', '.join(not_global)}. Mark these global first."
                ),
            )
        return
    if not slideshow_groups:
        return
    failures: list[str] = []
    for sid, src in sources_by_id.items():
        if src.is_global:
            continue
        sgroups = source_groups.get(sid, set())
        if not slideshow_groups.issubset(sgroups):
            failures.append(src.filename)
    if failures:
        raise HTTPException(
            status_code=400,
            detail=(
                "These source assets are not shared with all of the slideshow's "
                f"groups: {', '.join(sorted(failures))}. Share them (or mark "
                "global) first."
            ),
        )


def _compute_slideshow_duration_seconds(
    slides: list[SlideIn], sources_by_id: dict[uuid.UUID, Asset]
) -> float:
    """Sum slide durations for ``Asset.duration_seconds`` denormalisation.

    For ``play_to_end`` on a video, use the source's known media duration
    when available; otherwise fall back to the configured ``duration_ms``.
    Image slides always use the configured duration.
    """
    total_ms = 0.0
    for s in slides:
        src = sources_by_id[s.source_asset_id]
        if (
            s.play_to_end
            and src.asset_type == AssetType.VIDEO
            and src.duration_seconds
        ):
            total_ms += src.duration_seconds * 1000.0
        else:
            total_ms += s.duration_ms
    return total_ms / 1000.0


def _compute_slideshow_manifest_content_hash(
    slides: list[SlideIn], sources_by_id: dict[uuid.UUID, Asset]
) -> str:
    """Structural manifest content hash stored on ``Asset.checksum``.

    Hashes ordered slide structure plus each source asset's own checksum
    so that any change to the slide list (reorder, add/remove, durations,
    play_to_end) or to a source's content invalidates schedule pushes.
    The *resolved* per-device checksum (which additionally folds in the
    selected READY variant checksum for the device's profile) is computed
    at sync/resolve time on top of this base value.

    Renamed from ``_compute_slideshow_manifest_version`` to free up the
    word "version" for ``manifest_schema_version`` on the wire (the
    protocol-semver string added in agora#226 / slideshow wall-clock
    work). This hash and the schema version are independent concepts:
    the hash detects content edits, the schema version describes which
    fields the wire format carries.
    """
    h = hashlib.sha256()
    for idx, s in enumerate(slides):
        src = sources_by_id[s.source_asset_id]
        src_checksum = src.checksum or ""
        h.update(
            f"{idx}|{s.source_asset_id}|{src_checksum}|{s.duration_ms}|"
            f"{int(s.play_to_end)}|{s.transition}|{s.transition_ms}|".encode()
        )
    return h.hexdigest()


async def _revalidate_slideshow_audience(
    slideshow: Asset, db: AsyncSession
) -> None:
    """Re-check the ACL invariant for a slideshow after its audience changed.

    Used when sharing a slideshow with a new group or marking it global.
    Raises HTTPException(409) if any referenced source no longer covers
    the slideshow's effective audience, naming the offending sources.
    """
    rows = (
        await db.execute(
            select(SlideshowSlide.source_asset_id)
            .where(SlideshowSlide.slideshow_asset_id == slideshow.id)
            .distinct()
        )
    ).scalars().all()
    if not rows:
        return

    sources = (
        await db.execute(
            select(Asset).where(Asset.id.in_(rows), Asset.deleted_at.is_(None))
        )
    ).scalars().all()
    sources_by_id = {a.id: a for a in sources}

    if slideshow.is_global:
        not_global = sorted(s.filename for s in sources if not s.is_global)
        if not_global:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Cannot widen slideshow audience — these source assets "
                    f"are not global: {', '.join(not_global)}. Mark them "
                    "global first."
                ),
            )
        return

    slideshow_groups = {
        gid for (gid,) in (
            await db.execute(
                select(GroupAsset.group_id).where(
                    GroupAsset.asset_id == slideshow.id
                )
            )
        ).all()
    }
    if not slideshow_groups:
        return

    ga_rows = (
        await db.execute(
            select(GroupAsset.asset_id, GroupAsset.group_id).where(
                GroupAsset.asset_id.in_(rows)
            )
        )
    ).all()
    source_groups: dict[uuid.UUID, set[uuid.UUID]] = {sid: set() for sid in rows}
    for asset_id, group_id in ga_rows:
        source_groups[asset_id].add(group_id)

    failures: list[str] = []
    for sid, src in sources_by_id.items():
        if src.is_global:
            continue
        if not slideshow_groups.issubset(source_groups.get(sid, set())):
            failures.append(src.filename)
    if failures:
        raise HTTPException(
            status_code=409,
            detail=(
                "Cannot widen slideshow audience — these source assets are "
                f"not shared with all of the slideshow's groups: "
                f"{', '.join(sorted(failures))}. Share them (or mark "
                "global) first."
            ),
        )


@router.post("/slideshow", response_model=AssetOut, status_code=201)
async def create_slideshow_asset(
    request: Request,
    user: User = Depends(require_permission(ASSETS_WRITE)),
    db: AsyncSession = Depends(get_db),
):
    """Create a slideshow asset — a synthetic Asset whose content is an
    ordered list of existing image/video sources resolved on the device."""
    body = await request.json()

    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Slideshow name is required")
    if len(name) > 200:
        raise HTTPException(
            status_code=400, detail="Slideshow name too long (max 200 characters)"
        )

    raw_slides = body.get("slides")
    if not isinstance(raw_slides, list):
        raise HTTPException(status_code=400, detail="slides must be a list")
    try:
        slides = [SlideIn.model_validate(s) for s in raw_slides]
    except Exception as e:  # pydantic ValidationError or TypeError on bad shapes
        raise HTTPException(status_code=400, detail=f"Invalid slide payload: {e}")

    visible = await _visible_asset_ids(user, db)
    if slides:
        sources_by_id, source_groups = await _load_and_validate_slide_sources(
            slides, db, visible_ids=visible
        )
    else:
        # Allow a 0-slide draft slideshow. The AI assistant's create-mode
        # mint POSTs the builder's current in-memory slides, which is empty
        # for a brand-new slideshow; without this the create always 400s and
        # the assistant can never create a slideshow on a fresh page. The
        # empty draft is populated immediately by the assistant, and the
        # resolver treats a 0-slide slideshow as not-ready so it is never
        # pushed to a device.
        sources_by_id, source_groups = {}, {}

    # Resolve groups (mirror create_webpage_asset)
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

    # Defer the global/audience decision for an empty draft.  A 0-slide
    # slideshow has no sources, so there is no audience to enforce yet — and
    # minting it global (the admin default for empty groups) would lock the
    # audience to "global" before any source is chosen, making it impossible
    # for the assistant to then add the owner's non-global sources (the
    # global-source ACL would 400).  The audience is finalized later via the
    # builder's group/global picker (set_global action), which re-validates
    # the invariant against the now-known sources.
    make_global = (not resolved_groups and is_admin and bool(slides))

    _validate_slideshow_acl(
        set(resolved_groups), make_global, sources_by_id, source_groups
    )

    duration_seconds = _compute_slideshow_duration_seconds(slides, sources_by_id)
    manifest_content_hash = _compute_slideshow_manifest_content_hash(slides, sources_by_id)

    filename = await _unique_filename(db, name)

    asset = Asset(
        filename=filename,
        asset_type=AssetType.SLIDESHOW,
        size_bytes=0,
        checksum=manifest_content_hash,
        url=None,
        duration_seconds=duration_seconds,
        is_global=make_global,
        uploaded_by_user_id=user.id,
    )
    db.add(asset)
    await db.flush()

    for gid in resolved_groups:
        db.add(GroupAsset(asset_id=asset.id, group_id=gid))

    for idx, s in enumerate(slides):
        db.add(
            SlideshowSlide(
                slideshow_asset_id=asset.id,
                source_asset_id=s.source_asset_id,
                position=idx,
                duration_ms=s.duration_ms,
                play_to_end=s.play_to_end,
                transition=s.transition,
                transition_ms=s.transition_ms,
            )
        )

    await audit_log(
        db, user=user, action="asset.create_slideshow", resource_type="asset",
        resource_id=str(asset.id),
        description=f"Created slideshow asset '{filename}' with {len(slides)} slide(s)",
        details={
            "filename": filename,
            "slide_count": len(slides),
            "duration_seconds": duration_seconds,
            "group_ids": [str(g) for g in resolved_groups],
            "is_global": make_global,
        },
        request=request,
    )
    await db.commit()
    await db.refresh(asset)
    return asset


@router.get(
    "/{asset_id}/slides",
    dependencies=[Depends(require_permission(ASSETS_READ))],
)
async def list_slideshow_slides(
    asset_id: uuid.UUID,
    request: Request,
    profile_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Return ordered slides with embedded source metadata.

    When ``profile_id`` is provided, also include a ``readiness`` block
    enumerating any slides that can't be served on that profile (used by
    the builder UI and the assets table readiness badge).
    """
    await _verify_asset_access(asset_id, request, db)
    asset = (
        await db.execute(
            select(Asset).where(Asset.id == asset_id, Asset.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")
    if asset.asset_type != AssetType.SLIDESHOW:
        raise HTTPException(status_code=400, detail="Asset is not a slideshow")

    rows = (
        await db.execute(
            select(SlideshowSlide, Asset)
            .join(Asset, Asset.id == SlideshowSlide.source_asset_id)
            .where(SlideshowSlide.slideshow_asset_id == asset_id)
            .order_by(SlideshowSlide.position.asc())
        )
    ).all()

    src_thumb_map = await _thumbnail_urls_for([src.id for _, src in rows], db)

    slides_out = []
    for slide, src in rows:
        slides_out.append(
            {
                "id": str(slide.id),
                "position": slide.position,
                "duration_ms": slide.duration_ms,
                "play_to_end": slide.play_to_end,
                "transition": slide.transition,
                "transition_ms": slide.transition_ms,
                "source_asset_id": str(slide.source_asset_id),
                "source_filename": src.filename,
                "source_asset_type": src.asset_type.value,
                "source_duration_seconds": src.duration_seconds,
                "thumbnail_url": src_thumb_map.get(src.id),
            }
        )
    payload: dict = {"slideshow_id": str(asset_id), "slides": slides_out}
    if profile_id is not None:
        from cms.services.slideshow_resolver import slideshow_readiness
        payload["readiness"] = await slideshow_readiness(asset, profile_id, db)
    return payload


@router.put("/{asset_id}/slides")
async def replace_slideshow_slides(
    asset_id: uuid.UUID,
    request: Request,
    user: User = Depends(require_permission(ASSETS_WRITE)),
    db: AsyncSession = Depends(get_db),
):
    """Replace the entire slide list for a slideshow atomically."""
    await _verify_asset_access(asset_id, request, db)
    asset = (
        await db.execute(
            select(Asset).where(Asset.id == asset_id, Asset.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")
    if asset.asset_type != AssetType.SLIDESHOW:
        raise HTTPException(status_code=400, detail="Asset is not a slideshow")

    user_groups = await get_user_group_ids(user, db)
    is_admin = user_groups is None
    if not is_admin and asset.uploaded_by_user_id != user.id:
        raise HTTPException(
            status_code=403, detail="Only the slideshow owner can edit its slides"
        )

    body = await request.json()
    raw_slides = body.get("slides")
    if not isinstance(raw_slides, list):
        raise HTTPException(status_code=400, detail="slides must be a list")
    try:
        slides = [SlideIn.model_validate(s) for s in raw_slides]
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid slide payload: {e}")

    visible = await _visible_asset_ids(user, db)
    sources_by_id, source_groups = await _load_and_validate_slide_sources(
        slides, db, visible_ids=visible
    )

    existing_groups = {
        gid for (gid,) in (
            await db.execute(
                select(GroupAsset.group_id).where(GroupAsset.asset_id == asset_id)
            )
        ).all()
    }
    _validate_slideshow_acl(
        existing_groups, asset.is_global, sources_by_id, source_groups
    )

    duration_seconds = _compute_slideshow_duration_seconds(slides, sources_by_id)
    manifest_content_hash = _compute_slideshow_manifest_content_hash(slides, sources_by_id)

    await db.execute(
        delete(SlideshowSlide).where(SlideshowSlide.slideshow_asset_id == asset_id)
    )
    await db.flush()
    for idx, s in enumerate(slides):
        db.add(
            SlideshowSlide(
                slideshow_asset_id=asset_id,
                source_asset_id=s.source_asset_id,
                position=idx,
                duration_ms=s.duration_ms,
                play_to_end=s.play_to_end,
                transition=s.transition,
                transition_ms=s.transition_ms,
            )
        )
    asset.duration_seconds = duration_seconds
    asset.checksum = manifest_content_hash

    await audit_log(
        db, user=user, action="asset.replace_slides", resource_type="asset",
        resource_id=str(asset_id),
        description=(
            f"Replaced slides on slideshow '{asset.display_name or asset.original_filename or asset.filename}' "
            f"({len(slides)} slide(s))"
        ),
        details={
            "filename": asset.display_name or asset.original_filename or asset.filename,
            "slide_count": len(slides),
            "duration_seconds": duration_seconds,
        },
        request=request,
    )
    await db.commit()
    return {
        "slideshow_id": str(asset_id),
        "slide_count": len(slides),
        "duration_seconds": duration_seconds,
    }


async def _load_slideshow_for_write(
    asset_id: uuid.UUID, request: Request, db: AsyncSession
) -> Asset:
    """Load a slideshow asset, enforcing visibility + owner/admin write.

    Mirrors the gating of ``replace_slideshow_slides`` so the slideshow
    assistant-thread route and the chat re-validation hook share one
    rule. Returns the ``Asset`` or raises 404 / 403.
    """
    await _verify_asset_access(asset_id, request, db)
    asset = (
        await db.execute(
            select(Asset).where(Asset.id == asset_id, Asset.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")
    if asset.asset_type != AssetType.SLIDESHOW:
        raise HTTPException(status_code=400, detail="Asset is not a slideshow")

    # require_auth populates request.state.user for this router.
    user = getattr(request.state, "user", None)
    user_groups = await get_user_group_ids(user, db) if user else []
    is_admin = user_groups is None
    if not is_admin and (user is None or asset.uploaded_by_user_id != user.id):
        raise HTTPException(
            status_code=403, detail="Only the slideshow owner can edit its slides"
        )
    return asset


@router.post("/{asset_id}/assistant/thread")
async def slideshow_assistant_thread(
    asset_id: uuid.UUID,
    request: Request,
    user: User = Depends(require_permission(ASSETS_WRITE)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Get-or-create the editor-scoped AI chat thread for this slideshow.

    Powers the embedded chat panel in the slideshow builder. The thread
    is bound to ``asset_id`` and runs in ``slideshow_editor`` mode, which
    scopes the assistant to the slideshow read/write tools and forces
    every slideshow tool call onto *this* slideshow.

    Reuses the caller's newest existing ``slideshow_editor`` thread for
    the slideshow if one exists (so reopening the editor resumes the same
    conversation); otherwise creates one. Returns ``{thread_id, created}``.

    Gated identically to editing the slideshow itself: requires
    ``ASSETS_WRITE``, that the slideshow exists, is visible, and is owned
    by the caller (or the caller is an admin), and that the Assistant
    feature is enabled for the caller (404 otherwise, to keep the hidden
    feature invisible).
    """
    asset = await _load_slideshow_for_write(asset_id, request, db)

    if not await assistant_enabled_for(db, user):
        raise HTTPException(status_code=404, detail="Not found")

    existing = (
        await db.execute(
            select(ChatThread)
            .where(
                ChatThread.user_id == user.id,
                ChatThread.composed_asset_id == asset_id,
                ChatThread.mode == MODE_SLIDESHOW_EDITOR,
            )
            .order_by(ChatThread.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return {"thread_id": str(existing.id), "created": False}

    asset_name = asset.display_name or asset.filename or "slideshow"
    thread = ChatThread(
        user_id=user.id,
        mode=MODE_SLIDESHOW_EDITOR,
        composed_asset_id=asset_id,
        title=f"Editing {asset_name}"[:200],
    )
    db.add(thread)
    await db.commit()
    await db.refresh(thread)
    return {"thread_id": str(thread.id), "created": True}


@router.patch("/{asset_id}", response_model=AssetOut)
async def update_asset(
    asset_id: uuid.UUID,
    request: Request,
    user: User = Depends(require_permission(ASSETS_WRITE)),
    db: AsyncSession = Depends(get_db),
):
    """Update asset properties (currently: display_name)."""
    result = await db.execute(select(Asset).where(Asset.id == asset_id, Asset.deleted_at.is_(None)))
    asset = result.scalar_one_or_none()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")

    body = await request.json()

    # Build incoming updates dict from validated body fields, then diff
    incoming: dict = {}
    if "display_name" in body:
        name = (body["display_name"] or "").strip()
        if name and len(name) > 255:
            raise HTTPException(status_code=400, detail="Name too long (max 255 characters)")
        incoming["display_name"] = name if name else None

    if "url" in body:
        # URL editing is only supported for webpage assets. Stream URLs drive
        # the worker's capture pipeline and can't be mutated mid-flight;
        # saved-stream URL changes would require re-capture (use /recapture).
        if asset.asset_type != AssetType.WEBPAGE:
            raise HTTPException(
                status_code=400,
                detail="URL can only be edited on webpage assets",
            )
        new_url, _parsed = _validate_webpage_url(body["url"])
        incoming["url"] = new_url

    changes = compute_diff(asset, incoming)

    for field, value in incoming.items():
        setattr(asset, field, value)

    if changes:
        await audit_log(
            db, user=user, action="asset.update", resource_type="asset",
            resource_id=str(asset_id),
            description=f"Modified asset '{asset.display_name or asset.original_filename or asset.filename}'",
            details={"changes": changes},
            request=request,
        )
    await db.commit()
    await db.refresh(asset)

    # If a webpage asset's URL changed, re-snapshot its thumbnail. Best-effort.
    if "url" in changes and asset.asset_type == AssetType.WEBPAGE:
        try:
            from cms.services.transcoder import enqueue_thumbnail
            await enqueue_thumbnail(asset, db)
        except Exception:  # noqa: BLE001
            logger.warning(
                "Failed to re-enqueue webpage thumbnail for %s", asset.id,
                exc_info=True,
            )

    return asset


@router.post("/{asset_id}/recapture")
async def recapture_stream(
    asset_id: uuid.UUID,
    request: Request,
    user: User = Depends(require_permission(ASSETS_WRITE)),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """Re-capture a SAVED_STREAM asset: re-downloads the stream, overwrites
    the capture file, and resets all variants to PENDING for retranscoding."""
    result = await db.execute(select(Asset).where(Asset.id == asset_id, Asset.deleted_at.is_(None)))
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
    # Reset capture progress/error so the UI clears any stale
    # "Capture failed" state and shows the new capture from 0%.
    asset.capture_progress = None
    asset.capture_error = None

    # Delete variant files and reset to PENDING
    from cms.services.transcoder import cancel_asset_transcodes
    cancel_asset_transcodes(asset_id)

    variants_dir = settings.asset_storage_path / "variants"
    var_result = await db.execute(
        select(AssetVariant).where(AssetVariant.source_asset_id == asset_id)
    )
    reset_count = 0
    for variant in var_result.scalars().all():
        vpath = variants_dir / variant.filename
        if vpath.is_file():
            vpath.unlink()
        await storage.on_file_deleted(f"variants/{variant.filename}")
        variant.status = VariantStatus.PENDING
        variant.progress = 0.0
        variant.checksum = ""
        variant.size_bytes = 0
        reset_count += 1

    await db.commit()

    # Enqueue a STREAM_CAPTURE job.  The variants are already reset to
    # PENDING; after capture completes the monitor loop will enqueue
    # VARIANT_TRANSCODE jobs for them.
    from cms.services.transcoder import enqueue_stream_capture
    await enqueue_stream_capture(db, asset_id)

    await audit_log(
        db, user=user, action="asset.recapture", resource_type="asset",
        resource_id=str(asset_id),
        description=f"Recaptured saved-stream asset '{asset.display_name or asset.original_filename or asset.filename}'",
        details={"variants_reset": reset_count},
        request=request,
    )
    await db.commit()

    return {"recaptured": True, "asset_id": str(asset_id)}


@router.get("/{asset_id}/row", dependencies=[Depends(require_permission(ASSETS_READ))])
async def get_asset_row(asset_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)):
    """Return the rendered <tr class='asset-row'> + <tr class='asset-detail'>
    HTML for a single asset. Used by the no-reload flows on /assets (poller
    swap when variants first appear, cross-replica new-asset insert, upload
    complete, etc.) so the client never has to synthesize row markup in JS.
    See issue #87.
    """
    from fastapi.responses import HTMLResponse
    from cms.ui import templates
    from cms.services.variant_view import collapse_to_latest

    await _verify_asset_access(asset_id, request, db)

    result = await db.execute(
        select(Asset)
        .where(Asset.id == asset_id, Asset.deleted_at.is_(None))
        .options(selectinload(Asset.variants).selectinload(AssetVariant.profile))
    )
    asset = result.scalar_one_or_none()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")

    user: User | None = getattr(request.state, "user", None)
    user_perms = list(user.role.permissions) if user and user.role else []
    group_ids = await get_user_group_ids(user, db) if user else []
    is_admin = group_ids is None

    # Annotate the same way ui.assets_page does so the macro sees identical fields.
    # Skip thumbnail-purpose variants — they're CMS-internal.
    device_variants = [
        v for v in asset.variants
        if v.profile is None
        or getattr(v.profile, "purpose", "device") == "device"
    ]
    visible_variants = sorted(
        collapse_to_latest(device_variants),
        key=lambda v: (v.profile.name if v.profile else ""),
    )
    asset.visible_variants = visible_variants
    total = len(visible_variants)
    asset.variant_total = total
    asset.variant_ready = sum(1 for v in visible_variants if v.status == VariantStatus.READY)
    asset.variant_processing = sum(1 for v in visible_variants if v.status == VariantStatus.PROCESSING)
    asset.variant_failed = sum(1 for v in visible_variants if v.status == VariantStatus.FAILED)
    asset.variant_aggregate_progress = (
        round(sum((v.progress or 0.0) for v in visible_variants) / total, 1) if total > 0 else 0.0
    )
    sc = (await db.execute(
        select(func.count()).select_from(Schedule).where(Schedule.asset_id == asset.id)
    )).scalar() or 0
    asset.schedule_count = sc
    ga_rows = (await db.execute(
        select(GroupAsset).where(GroupAsset.asset_id == asset.id)
    )).scalars().all()
    if group_ids is not None:
        ga_rows = [ga for ga in ga_rows if ga.group_id in group_ids]
    asset.group_asset_entries = ga_rows

    # Groups the user can add the asset to + lookup for badge names.
    if group_ids is None:
        user_groups = (await db.execute(
            select(DeviceGroup).order_by(DeviceGroup.name)
        )).scalars().all()
        all_groups = (await db.execute(select(DeviceGroup))).scalars().all()
    elif group_ids:
        user_groups = (await db.execute(
            select(DeviceGroup).where(DeviceGroup.id.in_(group_ids)).order_by(DeviceGroup.name)
        )).scalars().all()
        all_groups = (await db.execute(
            select(DeviceGroup).where(DeviceGroup.id.in_(group_ids))
        )).scalars().all()
    else:
        user_groups = []
        all_groups = []
    group_name_map = {str(g.id): g.name for g in all_groups}

    uploader_map: dict[str, str] = {}
    if is_admin and asset.uploaded_by_user_id:
        row = (await db.execute(
            select(User.id, User.username, User.email).where(User.id == asset.uploaded_by_user_id)
        )).first()
        if row:
            uploader_map[str(row.id)] = row.username or row.email

    macros = templates.env.get_template("_macros.html").module
    thumbnail_url_map = await _thumbnail_urls_for([asset.id], db)
    tags_for = await _tags_for([asset.id], db)
    tags_map = {str(aid): [t.model_dump(mode="json") for t in tlist]
                for aid, tlist in tags_for.items()}
    usage_for = await _usage_for([asset.id], db)
    usage_map = {str(aid): u.model_dump(mode="json") for aid, u in usage_for.items()}
    html = macros.asset_row(
        asset,
        user_perms,
        is_admin,
        group_name_map,
        user_groups,
        uploader_map,
        user.id if user else None,
        thumbnail_url_map,
        tags_map,
        usage_map,
    )
    return HTMLResponse(str(html))


@router.get("/{asset_id}", response_model=AssetOut, dependencies=[Depends(require_permission(ASSETS_READ))])
async def get_asset(asset_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Asset).where(Asset.id == asset_id, Asset.deleted_at.is_(None)))
    asset = result.scalar_one_or_none()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")
    await _verify_asset_access(asset_id, request, db)
    thumbs = await _thumbnail_urls_for([asset.id], db)
    tags = await _tags_for([asset.id], db)
    return _asset_out_with_thumb(asset, thumbs, tags)


@device_router.get("/{asset_id}/download")
async def download_asset(
    asset_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    result = await db.execute(select(Asset).where(Asset.id == asset_id, Asset.deleted_at.is_(None)))
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
    result = await db.execute(select(Asset).where(Asset.id == asset_id, Asset.deleted_at.is_(None)))
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
    request: Request,
    user: User = Depends(require_permission(ASSETS_WRITE)),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """Soft-delete an asset.

    Sets ``assets.deleted_at`` and flags any in-flight Jobs targeting this
    asset (or its variants) with ``cancel_requested = true``.  The worker
    heartbeat will pick up the flag and abort ffmpeg within ~15s; the
    CMS reaper loop (``deleted_asset_reaper_loop``) later hard-deletes
    blobs + rows once all Jobs are terminal.  Returns 200 immediately.
    """
    from datetime import datetime, timezone
    from shared.models.job import Job, JobType, JobStatus

    result = await db.execute(
        select(Asset).where(Asset.id == asset_id, Asset.deleted_at.is_(None))
    )
    asset = result.scalar_one_or_none()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")

    # Only the owner or an admin can delete an asset
    user_groups = await get_user_group_ids(user, db)
    is_admin = user_groups is None
    if not is_admin and asset.uploaded_by_user_id != user.id:
        raise HTTPException(status_code=403, detail="Only the asset owner can delete this asset")

    # Block source-asset deletion while any slideshow still references it.
    # The FK is RESTRICT, so even soft-deleted slideshows would block the
    # reaper's hard-delete pass; surface that to the user up-front with the
    # offending slideshow filenames (active vs soft-deleted called out).
    if asset.asset_type in (AssetType.IMAGE, AssetType.VIDEO):
        slide_refs = (
            await db.execute(
                select(SlideshowSlide.slideshow_asset_id, Asset.filename, Asset.deleted_at)
                .join(Asset, Asset.id == SlideshowSlide.slideshow_asset_id)
                .where(SlideshowSlide.source_asset_id == asset_id)
            )
        ).all()
        if slide_refs:
            active_names = sorted({r[1] for r in slide_refs if r[2] is None})
            soft_deleted_names = sorted({r[1] for r in slide_refs if r[2] is not None})
            parts = []
            if active_names:
                parts.append(f"active slideshow(s): {', '.join(active_names)}")
            if soft_deleted_names:
                parts.append(
                    "soft-deleted slideshow(s) pending reap: "
                    + ", ".join(soft_deleted_names)
                )
            raise HTTPException(
                status_code=409,
                detail=(
                    "Cannot delete — asset is referenced by " + "; ".join(parts) + "."
                ),
            )

    # Block deletion if ANY non-expired schedule references this asset.
    # We treat "disabled but not yet expired" as still in use, because
    # disabling a schedule is an intentional, typically temporary action
    # by the user — hard-deleting the asset out from under it would make
    # later re-enabling silently fail. Expired schedules (end_date in
    # the past) don't block deletion and their rows are cleaned up below.
    now_utc = datetime.now(timezone.utc)
    blocking_sched_count = await db.scalar(
        select(func.count()).select_from(Schedule).where(
            Schedule.asset_id == asset_id,
            (Schedule.end_date.is_(None)) | (Schedule.end_date >= now_utc),
        )
    )
    if blocking_sched_count:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Cannot delete -- asset is referenced by {blocking_sched_count} "
                "non-expired schedule(s) (enabled or disabled). Delete or expire "
                "those schedules first."
            ),
        )

    # Remove any remaining (expired/disabled) schedule rows that reference
    # this asset.  Schedule.asset_id is NOT NULL, so we can't null it —
    # and since those schedules are all inactive, dropping them is safe
    # and prevents an FK violation when the reaper hard-deletes the row.
    stale_sched_count = await db.scalar(
        select(func.count()).select_from(Schedule).where(Schedule.asset_id == asset_id)
    )
    if stale_sched_count:
        await db.execute(delete(Schedule).where(Schedule.asset_id == asset_id))

    asset_filename = asset.filename
    # Friendly, user-facing name for the audit log (the raw filename is a
    # GUID storage name, e.g. composed-<uuid>-<hash>.html).
    asset_display_name = (
        asset.display_name or asset.original_filename or asset.filename
    )

    # Mark as soft-deleted
    asset.deleted_at = datetime.now(timezone.utc)

    # Flag all active jobs for cancellation AND mark them terminal.  Jobs
    # are polymorphic:
    #   - VARIANT_TRANSCODE.target_id → asset_variants.id (join through variants)
    #   - STREAM_CAPTURE.target_id    → assets.id (direct)
    #
    # Setting status=CANCELLED here (rather than just cancel_requested=True)
    # ensures the reaper can hard-delete promptly even in LISTEN/NOTIFY mode,
    # where the worker doesn't otherwise transition Job rows out of
    # PENDING/PROCESSING after the variant finishes.  The worker's
    # cancel-probe (in _transcode_one / _capture_stream) still aborts any
    # in-flight ffmpeg mid-run; if it races us and marks DONE/FAILED first,
    # those are also terminal — harmless overwrite.
    variant_ids_subq = (
        select(AssetVariant.id).where(AssetVariant.source_asset_id == asset_id)
    ).scalar_subquery()

    active_statuses = [JobStatus.PENDING, JobStatus.PROCESSING]
    await db.execute(
        update(Job)
        .where(
            Job.status.in_(active_statuses),
            (
                (Job.type == JobType.VARIANT_TRANSCODE) & (Job.target_id.in_(variant_ids_subq))
            ) | (
                (Job.type == JobType.STREAM_CAPTURE) & (Job.target_id == asset_id)
            ),
        )
        .values(
            cancel_requested=True,
            status=JobStatus.CANCELLED,
            error_message="Asset deleted by user",
        )
    )

    await audit_log(
        db, user=user, action="asset.delete", resource_type="asset",
        resource_id=str(asset_id),
        description=f"Soft-deleted asset '{asset_display_name}'",
        details={"filename": asset_display_name, "asset_type": asset.asset_type.value},
        request=request,
    )
    await db.commit()
    return {"deleted": asset_filename, "soft_delete": True}


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
    asset = (await db.execute(select(Asset).where(Asset.id == asset_id, Asset.deleted_at.is_(None)))).scalar_one_or_none()
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

    # ACL invariant when sharing a SLIDESHOW with a new group: every
    # referenced source asset must already be visible to that group, or
    # users in the group could reach the source through the slideshow
    # without being authorised on the source directly.  Roll back the
    # newly-added GroupAsset row if the invariant fails.
    if asset.asset_type == AssetType.SLIDESHOW:
        try:
            await _revalidate_slideshow_audience(asset, db)
        except HTTPException:
            await db.rollback()
            raise

    # Enrich audit log with uploader context so admins can trace content
    # propagation (issue #176): original uploader, filename, target group.
    uploader_email: str | None = None
    if asset.uploaded_by_user_id is not None:
        uploader_email = await db.scalar(
            select(User.email).where(User.id == asset.uploaded_by_user_id)
        )
    await audit_log(
        db, user=user, action="asset.share", resource_type="asset",
        resource_id=str(asset_id),
        description=f"Shared asset '{asset.display_name or asset.original_filename or asset.filename}' with group '{group.name}'",
        details={
            "asset_filename": asset.display_name or asset.original_filename or asset.filename,
            "group_id": str(group_id),
            "group_name": group.name,
            "uploaded_by_user_id": (
                str(asset.uploaded_by_user_id)
                if asset.uploaded_by_user_id is not None else None
            ),
            "uploaded_by_email": uploader_email,
        },
        request=request,
    )
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

    # ACL invariant: refuse to unshare a source asset from a group while a
    # non-deleted slideshow scoped to that group still references it and
    # would lose visibility on the source.  The slideshow's audience must
    # remain a subset of every source's audience.
    blocking_slideshows = (
        await db.execute(
            select(Asset.filename)
            .join(SlideshowSlide, SlideshowSlide.slideshow_asset_id == Asset.id)
            .join(GroupAsset, GroupAsset.asset_id == Asset.id)
            .where(
                SlideshowSlide.source_asset_id == asset_id,
                GroupAsset.group_id == group_id,
                Asset.deleted_at.is_(None),
                Asset.is_global.is_(False),
            )
            .distinct()
        )
    ).scalars().all()
    if blocking_slideshows:
        raise HTTPException(
            status_code=409,
            detail=(
                "Cannot unshare — source asset is referenced by slideshow(s) "
                f"sharing this group: {', '.join(sorted(blocking_slideshows))}. "
                "Remove the source from those slideshows first."
            ),
        )

    await db.delete(ga)

    # Load asset + group + uploader context for a richer audit log entry
    # (issue #176). Best-effort — audit details shouldn't block the unshare.
    asset_row = (await db.execute(
        select(Asset).where(Asset.id == asset_id)
    )).scalar_one_or_none()
    group_row = (await db.execute(
        select(DeviceGroup).where(DeviceGroup.id == group_id)
    )).scalar_one_or_none()
    asset_filename = (
        (asset_row.display_name or asset_row.original_filename or asset_row.filename)
        if asset_row is not None else None
    )
    group_name = group_row.name if group_row is not None else None
    uploader_email: str | None = None
    uploader_id = asset_row.uploaded_by_user_id if asset_row is not None else None
    if uploader_id is not None:
        uploader_email = await db.scalar(
            select(User.email).where(User.id == uploader_id)
        )
    description = (
        f"Unshared asset '{asset_filename}' from group '{group_name}'"
        if asset_filename and group_name
        else f"Unshared asset {asset_id} from group {group_id}"
    )
    await audit_log(
        db, user=user, action="asset.unshare", resource_type="asset",
        resource_id=str(asset_id),
        description=description,
        details={
            "asset_filename": asset_filename,
            "group_id": str(group_id),
            "group_name": group_name,
            "uploaded_by_user_id": (
                str(uploader_id) if uploader_id is not None else None
            ),
            "uploaded_by_email": uploader_email,
        },
        request=request,
    )
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
    asset = (await db.execute(select(Asset).where(Asset.id == asset_id, Asset.deleted_at.is_(None)))).scalar_one_or_none()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")

    # ACL invariant when un-globalising a source asset: any non-deleted
    # GLOBAL slideshow referencing it would suddenly have a wider audience
    # than its source, which is a leak.  Block the toggle and tell the
    # user which slideshows need their global flag dropped (or the source
    # removed) first.
    if asset.is_global and asset.asset_type in (AssetType.IMAGE, AssetType.VIDEO):
        blocking_slideshows = (
            await db.execute(
                select(Asset.filename)
                .join(SlideshowSlide, SlideshowSlide.slideshow_asset_id == Asset.id)
                .where(
                    SlideshowSlide.source_asset_id == asset_id,
                    Asset.deleted_at.is_(None),
                    Asset.is_global.is_(True),
                )
                .distinct()
            )
        ).scalars().all()
        if blocking_slideshows:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Cannot unmark global — asset is referenced by global "
                    f"slideshow(s): {', '.join(sorted(blocking_slideshows))}. "
                    "Remove the source from those slideshows or unmark them "
                    "global first."
                ),
            )

    asset.is_global = not asset.is_global

    # ACL invariant when marking a SLIDESHOW global: every referenced
    # source asset must already be global, otherwise users without group
    # membership could reach a non-global source through the now-global
    # slideshow.  Validated after the toggle so error reporting in the
    # helper sees the post-toggle state.
    if asset.is_global and asset.asset_type == AssetType.SLIDESHOW:
        try:
            await _revalidate_slideshow_audience(asset, db)
        except HTTPException:
            await db.rollback()
            raise
    await audit_log(
        db, user=getattr(request.state, "user", None),
        action="asset.toggle_global", resource_type="asset",
        resource_id=str(asset_id),
        description=f"{'Marked' if asset.is_global else 'Unmarked'} asset '{asset.display_name or asset.original_filename or asset.filename}' as global",
        details={"is_global": asset.is_global},
        request=request,
    )
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
