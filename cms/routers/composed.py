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

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cms.auth import get_user_group_ids, require_auth, require_permission
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
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    """Live-render the composed slide bound to ``asset_id`` as HTML.

    Returns 404 if the asset doesn't exist or isn't a composed slide.
    Returns 422 if the saved layout fails Pydantic shape validation
    or the bundle builder's semantic validation. No disk writes;
    no asset row mutations.
    """
    asset = await db.get(Asset, asset_id)
    if asset is None or asset.asset_type != AssetType.COMPOSED:
        raise HTTPException(status_code=404, detail="Composed slide not found")

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

    # Trigger auto-registration of all built-in widgets, then build.
    import cms.composed.widgets  # noqa: F401

    registry = get_registry()
    try:
        built = build_bundle(layout, registry)
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
