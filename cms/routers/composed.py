"""Composed-slide HTTP API + Phase 1A live-preview endpoint.

Routes:

* ``GET  /composed/{asset_id}/preview`` — live HTML render (Phase 1A).
* ``POST /composed/`` — create a new draft composed-slide asset.
* ``PATCH /composed/{asset_id}/layout`` — replace the layout JSON.
* ``POST /composed/{asset_id}/publish`` — build the bundle and clear
  ``is_draft``.

All write routes require ``ASSETS_WRITE``. Visibility is enforced via
``_verify_asset_access`` (the asset-library's shared visibility rule),
and PATCH additionally checks that referenced assets are visible to
the same audience as the composed slide (slideshow-style ACL).

CSP is locked down on the preview response: only the inline content
the bundle builder generates is allowed; no external scripts, styles,
fonts, or images. Frame ancestors are restricted to the CMS origin so
the preview can render inside an editor ``<iframe>`` but cannot be
embedded elsewhere.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import mimetypes
import uuid
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cms.auth import (
    get_settings,
    get_user_group_ids,
    require_auth,
    require_permission,
)
from cms.composed.bundle import BundleValidationError, build_bundle
from cms.composed.registry import get_registry
from cms.composed.schema import Layout, empty_layout
from cms.composed.validate import validate_layout
from cms.database import get_db
from cms.models.asset import Asset, AssetType
from cms.models.composed_slide import ComposedSlide
from cms.models.group_asset import GroupAsset
from cms.models.user import User
from cms.permissions import ASSETS_WRITE
from cms.services.audit_service import audit_log

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/composed",
    dependencies=[Depends(require_auth)],
    tags=["composed"],
)


_PREVIEW_CSP = (
    "default-src 'none'; "
    "script-src 'unsafe-inline'; "
    "style-src 'unsafe-inline'; "
    "img-src data:; "
    "font-src data:; "
    "media-src data:; "
    "frame-ancestors 'self'; "
    "base-uri 'none'; "
    "form-action 'none'"
)


# Widgets allowed to render a VIDEO asset. The ImageWidget only knows
# how to emit an <img> from inline bytes, so routing a video to it would
# raise at render time (HTTP 500); restrict video to the media widget.
_VIDEO_CAPABLE_SLUGS = {"media"}

# Hard cap on a video inlined as a base64 data: URI in the *preview*
# response. The locked-down preview CSP only permits ``media-src data:``,
# so the device-local /assets/videos sibling URL the publish path uses
# can't load in a browser — preview must inline the bytes. To avoid
# reading an arbitrarily large file into memory (and base64-bloating it
# by ~33%), refuse anything over this size with a clean 422.
_MAX_INLINE_VIDEO_BYTES = 32 * 1024 * 1024


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────


async def _load_composed_for_write(
    asset_id: uuid.UUID, request: Request, db: AsyncSession,
) -> tuple[Asset, ComposedSlide]:
    """Look up the asset + backing composed-slide row, enforcing visibility.

    Returns ``(asset, composed)`` or raises 404 / 403.
    """
    asset = await db.get(Asset, asset_id)
    if asset is None or asset.asset_type != AssetType.COMPOSED or asset.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Composed slide not found")

    # Visibility / ownership check. Reuse the assets router's helper so
    # the rule stays single-sourced.
    from cms.routers.assets import _verify_asset_access  # noqa: WPS433

    await _verify_asset_access(asset_id, request, db)

    cs_result = await db.execute(
        select(ComposedSlide).where(ComposedSlide.asset_id == asset_id),
    )
    composed = cs_result.scalar_one_or_none()
    if composed is None:
        raise HTTPException(status_code=404, detail="Composed slide not found")
    return asset, composed


async def _check_referenced_asset_acl(
    layout: Layout,
    composed_asset: Asset,
    db: AsyncSession,
) -> None:
    """Reject layouts that reference an asset the audience can't see.

    Mirrors :func:`cms.routers.assets._validate_slideshow_acl` for the
    composed case. Every asset declared by any widget in the layout
    must be visible to a superset of the composed slide's audience:

    * Composed slide is global → every referenced asset must also be global.
    * Composed slide is group-scoped → every referenced asset must be
      global, or shared with a superset of the composed slide's group set.
    * Composed slide is personal (no groups, not global) → no extra
      audience-widening check; visibility is already enforced via
      ``_verify_asset_access``.
    """
    # Trigger widget registration so we can ask each widget for declared IDs.
    import cms.composed.widgets  # noqa: F401, WPS433

    registry = get_registry()
    referenced: set[uuid.UUID] = set()
    for inst in layout.widgets:
        try:
            widget = registry.get(inst.type)
        except KeyError:
            # Unknown widget type — leave it for validate_layout to
            # surface a clean error.
            continue
        try:
            cfg = widget.ConfigSchema.model_validate(inst.config)
        except Exception:  # noqa: BLE001
            # Config-shape errors are reported separately by
            # validate_layout; ignore for ACL purposes.
            continue
        for aid in widget.declared_asset_ids(cfg):
            referenced.add(aid)

    if not referenced:
        return

    # Load each referenced asset + the groups it's shared with.
    asset_rows = (await db.execute(
        select(Asset).where(
            Asset.id.in_(referenced), Asset.deleted_at.is_(None),
        )
    )).scalars().all()
    found_ids = {a.id for a in asset_rows}
    missing = referenced - found_ids
    if missing:
        raise HTTPException(
            status_code=400,
            detail=(
                "Referenced asset(s) not found: "
                + ", ".join(str(a) for a in sorted(missing))
            ),
        )

    group_rows = (await db.execute(
        select(GroupAsset.asset_id, GroupAsset.group_id).where(
            GroupAsset.asset_id.in_(referenced),
        )
    )).all()
    source_groups: dict[uuid.UUID, set[uuid.UUID]] = {}
    for aid, gid in group_rows:
        source_groups.setdefault(aid, set()).add(gid)

    composed_group_rows = (await db.execute(
        select(GroupAsset.group_id).where(GroupAsset.asset_id == composed_asset.id)
    )).scalars().all()
    composed_groups: set[uuid.UUID] = set(composed_group_rows)

    if composed_asset.is_global:
        not_global = sorted(a.filename for a in asset_rows if not a.is_global)
        if not_global:
            raise HTTPException(
                status_code=400,
                detail=(
                    "A global composed slide can only reference global assets. "
                    f"Not global: {', '.join(not_global)}. Mark these global first."
                ),
            )
        return

    if not composed_groups:
        # Personal slide — visibility was already enforced upstream.
        return

    failures: list[str] = []
    for a in asset_rows:
        if a.is_global:
            continue
        sgroups = source_groups.get(a.id, set())
        if not composed_groups.issubset(sgroups):
            failures.append(a.filename)
    if failures:
        raise HTTPException(
            status_code=400,
            detail=(
                "These referenced assets are not shared with all of the composed "
                f"slide's groups: {', '.join(sorted(failures))}. Share them (or "
                "mark global) first."
            ),
        )


# ─────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────


@router.get("/{asset_id}/preview", response_class=HTMLResponse)
async def preview_composed_slide(
    asset_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    settings=Depends(get_settings),
) -> HTMLResponse:
    """Live-render the composed slide bound to ``asset_id`` as HTML.

    Returns 404 if the asset doesn't exist or isn't a composed slide.
    Returns 403 if the caller can't see the slide (or any asset it
    references). Returns 422 if the saved layout fails validation, a
    referenced asset is missing / the wrong type, or an inlined video
    exceeds the preview size cap. No disk writes; no row mutations.

    Unlike the publish path (which ships videos as device-local sibling
    cache files), preview inlines every referenced image *and* video as
    a base64 ``data:`` URI so the render works in an editor ``<iframe>``
    under the locked-down preview CSP.
    """
    asset = await db.get(Asset, asset_id)
    if (
        asset is None
        or asset.asset_type != AssetType.COMPOSED
        or asset.deleted_at is not None
    ):
        raise HTTPException(status_code=404, detail="Composed slide not found")

    # Visibility / ownership gate for the slide itself (was missing —
    # preview previously had no per-asset access check at all).
    from cms.routers.assets import _verify_asset_access  # noqa: WPS433

    await _verify_asset_access(asset_id, request, db)

    cs_result = await db.execute(
        select(ComposedSlide).where(ComposedSlide.asset_id == asset_id),
    )
    composed = cs_result.scalar_one_or_none()
    if composed is None:
        raise HTTPException(status_code=404, detail="Composed slide not found")

    try:
        layout = Layout.model_validate(composed.layout_json)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            status_code=422, detail=f"Invalid layout JSON: {e}",
        ) from e

    # Trigger auto-registration of all built-in widgets.
    import cms.composed.widgets  # noqa: F401

    registry = get_registry()

    # Run the semantic validator first so unknown widgets / bad configs
    # surface a clean 422 before we start touching assets.
    layout_errors = validate_layout(layout, registry)
    if layout_errors:
        raise HTTPException(
            status_code=422,
            detail=(
                "Layout failed validation: "
                + "; ".join(f"{err.code}: {err.message}" for err in layout_errors)
            ),
        )

    # Collect every asset declared by the layout, tracking which widget
    # slug(s) declared each so we can enforce type compatibility.
    declared_ids: list[uuid.UUID] = []
    seen: set[uuid.UUID] = set()
    declaring_slugs: dict[uuid.UUID, set[str]] = {}
    for inst in layout.widgets:
        widget = registry.get(inst.type)
        if widget is None:
            continue
        try:
            cfg = widget.ConfigSchema.model_validate(inst.config)
        except Exception:  # noqa: BLE001 — shape errors already 422'd above
            continue
        for aid in widget.declared_asset_ids(cfg):
            if aid not in seen:
                seen.add(aid)
                declared_ids.append(aid)
            declaring_slugs.setdefault(aid, set()).add(inst.type)

    asset_payloads: dict[uuid.UUID, tuple[bytes, str]] = {}
    sibling_asset_urls: dict[uuid.UUID, str] = {}

    if declared_ids:
        storage_dir = settings.asset_storage_path
        rows = await db.execute(
            select(Asset).where(
                Asset.id.in_(declared_ids), Asset.deleted_at.is_(None),
            ),
        )
        by_id = {a.id: a for a in rows.scalars().all()}

        for aid in declared_ids:
            ref = by_id.get(aid)
            if ref is None:
                raise HTTPException(
                    status_code=422,
                    detail=f"Composed slide references missing asset {aid}",
                )

            # Per-referenced-asset visibility check — a viewer who can see
            # the slide must not be able to inline an asset they can't see.
            await _verify_asset_access(aid, request, db)

            if ref.asset_type == AssetType.IMAGE:
                blob = await _read_inline_asset(storage_dir, ref)
                mime, _ = mimetypes.guess_type(ref.filename)
                asset_payloads[aid] = (blob, mime or "application/octet-stream")
            elif ref.asset_type == AssetType.VIDEO:
                slugs = declaring_slugs.get(aid, set())
                if not slugs.issubset(_VIDEO_CAPABLE_SLUGS):
                    bad = sorted(slugs - _VIDEO_CAPABLE_SLUGS)
                    raise HTTPException(
                        status_code=422,
                        detail=(
                            f"Asset {aid} is a video but is used by widget(s) "
                            f"{', '.join(bad)} that cannot render video"
                        ),
                    )
                # Fast reject on recorded size before reading anything.
                if (ref.size_bytes or 0) > _MAX_INLINE_VIDEO_BYTES:
                    raise HTTPException(
                        status_code=422,
                        detail=(
                            f"Video asset {aid} is too large to preview "
                            f"({ref.size_bytes} bytes; cap "
                            f"{_MAX_INLINE_VIDEO_BYTES})"
                        ),
                    )
                blob = await _read_inline_asset(
                    storage_dir, ref, max_bytes=_MAX_INLINE_VIDEO_BYTES,
                )
                mime, _ = mimetypes.guess_type(ref.filename)
                b64 = base64.b64encode(blob).decode("ascii")
                sibling_asset_urls[aid] = f"data:{mime or 'video/mp4'};base64,{b64}"
            else:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"Composed slide references asset {aid} of type "
                        f"{ref.asset_type.value!r}; only IMAGE and VIDEO "
                        "assets can be embedded in a composed slide"
                    ),
                )

    def _asset_loader(aid: uuid.UUID) -> tuple[bytes, str]:
        return asset_payloads[aid]

    try:
        built = build_bundle(
            layout,
            registry,
            asset_loader=_asset_loader if asset_payloads else None,
            sibling_asset_urls=sibling_asset_urls or None,
        )
    except BundleValidationError as e:
        raise HTTPException(
            status_code=422,
            detail=(
                "Layout failed validation: "
                + "; ".join(f"{err.code}: {err.message}" for err in e.errors)
            ),
        ) from e

    headers = {
        "Content-Security-Policy": _PREVIEW_CSP,
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "no-store",
    }
    return HTMLResponse(content=built.html_bytes, headers=headers)


async def _read_inline_asset(
    storage_dir, ref: Asset, *, max_bytes: int | None = None,
) -> bytes:
    """Read a referenced asset's bytes for inlining into a preview.

    Enforces that the resolved path stays under ``storage_dir``
    (defense-in-depth against a crafted filename) and, for videos, that
    the on-disk size — checked via ``stat`` *before* reading — is within
    ``max_bytes``. Raises 422 on any read / size problem.
    """
    base = storage_dir.resolve()
    path = (storage_dir / ref.filename).resolve()
    if not path.is_relative_to(base):
        raise HTTPException(
            status_code=422,
            detail=f"Asset {ref.id} has an invalid storage path",
        )
    try:
        if max_bytes is not None:
            actual = path.stat().st_size
            if actual > max_bytes:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"Video asset {ref.id} is too large to preview "
                        f"({actual} bytes; cap {max_bytes})"
                    ),
                )
            # Bounded read closes the stat→read TOCTOU window: if the file
            # grew/was swapped after the stat, refuse rather than inline
            # more than the cap.
            blob = await asyncio.to_thread(_read_capped, path, max_bytes)
            if blob is None:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"Video asset {ref.id} is too large to preview "
                        f"(exceeds cap {max_bytes})"
                    ),
                )
            return blob
        return await asyncio.to_thread(path.read_bytes)
    except HTTPException:
        raise
    except OSError as e:
        raise HTTPException(
            status_code=422,
            detail=f"Asset {ref.id} file missing or unreadable: {ref.filename}",
        ) from e


def _read_capped(path, max_bytes: int) -> bytes | None:
    """Read up to ``max_bytes``; return None if the file is larger."""
    with path.open("rb") as fh:
        data = fh.read(max_bytes + 1)
    if len(data) > max_bytes:
        return None
    return data


@router.post("/", status_code=201)
async def create_composed_slide(
    request: Request,
    body: dict[str, Any] = Body(...),
    user: User = Depends(require_permission(ASSETS_WRITE)),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Create a new draft composed-slide asset with an empty layout.

    Body:
        ``name`` (str, required): user-facing display name; also used as
            the unique ``Asset.filename`` (with a numeric suffix if a
            conflict exists).
        ``group_ids`` (list[str] | str, optional): group UUIDs to share
            the asset with. Empty list + admin → global.

    Returns the created asset's id and a redirect-friendly editor URL.
    """
    # Local imports keep this router off the assets.py hot path at startup.
    from cms.routers.assets import _unique_filename  # noqa: WPS433

    name_raw = body.get("name") or body.get("display_name")
    if not name_raw or not str(name_raw).strip():
        raise HTTPException(status_code=400, detail="name is required")
    name = str(name_raw).strip()
    if len(name) > 255:
        raise HTTPException(status_code=400, detail="name must be ≤255 chars")

    user_groups = await get_user_group_ids(user, db)
    is_admin = user_groups is None

    raw_ids = body.get("group_ids", [])
    if isinstance(raw_ids, str):
        raw_ids = [g.strip() for g in raw_ids.split(",") if g.strip()]
    single = body.get("group_id")
    if single and not raw_ids:
        raw_ids = [single]

    resolved_groups: list[uuid.UUID] = []
    for gid in raw_ids:
        try:
            parsed_id = uuid.UUID(str(gid))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid group_id: {gid}")
        if not is_admin and parsed_id not in (user_groups or []):
            raise HTTPException(status_code=403, detail="You are not a member of this group")
        resolved_groups.append(parsed_id)

    make_global = (not resolved_groups and is_admin)

    filename = await _unique_filename(db, name)

    asset = Asset(
        filename=filename,
        display_name=name,
        asset_type=AssetType.COMPOSED,
        size_bytes=0,
        checksum="",
        url=None,
        is_global=make_global,
        uploaded_by_user_id=user.id,
    )
    db.add(asset)
    await db.flush()

    for gid in resolved_groups:
        db.add(GroupAsset(asset_id=asset.id, group_id=gid))

    layout = empty_layout()
    composed = ComposedSlide(
        asset_id=asset.id,
        layout_json=layout.model_dump(mode="json"),
        schema_version=layout.schema_version,
        is_draft=True,
    )
    db.add(composed)

    await audit_log(
        db,
        user=user,
        action="asset.create_composed",
        resource_type="asset",
        resource_id=str(asset.id),
        description=f"Created composed slide '{filename}'",
        details={
            "filename": filename,
            "group_ids": [str(g) for g in resolved_groups],
            "is_global": make_global,
        },
        request=request,
    )
    await db.commit()
    await db.refresh(asset)
    return {
        "id": str(asset.id),
        "filename": asset.filename,
        "display_name": asset.display_name,
        "edit_url": f"/assets/{asset.id}/composed",
    }


@router.patch("/{asset_id}/layout")
async def patch_composed_layout(
    asset_id: uuid.UUID,
    request: Request,
    body: dict[str, Any] = Body(...),
    user: User = Depends(require_permission(ASSETS_WRITE)),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Replace the layout JSON for a composed slide.

    Body must be the full canonical ``Layout`` JSON (Pydantic shape).
    The endpoint:

    1. Validates Pydantic shape — 422 on failure.
    2. Runs ``validate_layout`` (semantic: bounds, overlaps, unknown
       widget types, per-widget ``validate_semantic``) — 422 on failure.
    3. Checks referenced-asset ACL (audience widening) — 400 on failure.
    4. Persists the canonical JSON via ``layout.model_dump(mode="json")``.
    5. Sets ``is_draft=True`` so any previously-published bundle is
       flagged stale until ``/publish`` is called again.
    """
    asset, composed = await _load_composed_for_write(asset_id, request, db)

    # Trigger widget registration before validate_layout uses the registry.
    import cms.composed.widgets  # noqa: F401, WPS433

    try:
        layout = Layout.model_validate(body)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            status_code=422,
            detail={"error": "invalid_layout_shape", "message": str(e)},
        ) from e

    registry = get_registry()
    errors = validate_layout(layout, registry)
    if errors:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "invalid_layout",
                "errors": [
                    {
                        "code": err.code,
                        "message": err.message,
                        "widget_id": err.widget_id,
                    }
                    for err in errors
                ],
            },
        )

    await _check_referenced_asset_acl(layout, asset, db)

    composed.layout_json = layout.model_dump(mode="json")
    composed.schema_version = layout.schema_version
    composed.is_draft = True

    await audit_log(
        db,
        user=user,
        action="asset.update_composed_layout",
        resource_type="asset",
        resource_id=str(asset.id),
        description=f"Updated composed-slide layout for '{asset.filename}'",
        details={"widget_count": len(layout.widgets)},
        request=request,
    )
    await db.commit()
    return {
        "id": str(asset.id),
        "is_draft": True,
        "widget_count": len(layout.widgets),
    }


@router.post("/{asset_id}/publish")
async def publish_composed_endpoint(
    asset_id: uuid.UUID,
    request: Request,
    user: User = Depends(require_permission(ASSETS_WRITE)),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Build the bundle for the composed slide and clear ``is_draft``.

    Returns the bundle filename, size, and sha256. Errors map to:

    * 422 — empty layout (need at least one widget).
    * 422 — bundle/validation error (``PublishError``).
    """
    from cms.composed.publish import PublishError, publish_composed_slide  # noqa: WPS433

    asset, composed = await _load_composed_for_write(asset_id, request, db)

    # Cheap pre-check so the user gets a friendly error instead of the
    # bundle builder's generic "nothing to render" message.
    try:
        layout = Layout.model_validate(composed.layout_json)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            status_code=422,
            detail={"error": "invalid_layout_shape", "message": str(e)},
        ) from e
    if not layout.widgets:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "empty_layout",
                "message": "Add at least one widget before publishing.",
            },
        )

    # Trigger widget registration before publish_composed_slide reads it.
    import cms.composed.widgets  # noqa: F401, WPS433

    try:
        result = await publish_composed_slide(asset_id, db)
    except PublishError as e:
        raise HTTPException(
            status_code=422,
            detail={"error": "publish_failed", "message": str(e)},
        ) from e

    await audit_log(
        db,
        user=user,
        action="asset.publish_composed",
        resource_type="asset",
        resource_id=str(asset.id),
        description=f"Published composed slide '{asset.filename}'",
        details={
            "filename": result.filename,
            "size_bytes": result.size_bytes,
            "checksum": result.checksum,
        },
        request=request,
    )
    await db.commit()
    await db.refresh(asset)
    return {
        "id": str(asset.id),
        "filename": result.filename,
        "size_bytes": result.size_bytes,
        "checksum": result.checksum,
        "rebuilt": result.rebuilt,
        "is_draft": False,
    }
