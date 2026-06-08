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

from cms.auth import (
    get_settings,
    get_user_group_ids,
    require_auth,
    require_permission,
)
from cms.composed.registry import get_registry
from cms.composed.render import build_composed_html
from cms.composed.schema import (
    CANVAS_HEIGHT,
    CANVAS_WIDTH,
    GRID_COLS,
    GRID_ROWS,
    Layout,
    empty_layout,
)
from cms.composed.validate import validate_layout
from cms.database import get_db
from cms.models.asset import Asset, AssetType
from cms.models.composed_slide import ComposedSlide
from cms.models.chat_thread import ChatThread
from cms.models.group_asset import GroupAsset
from cms.models.user import User
from cms.permissions import ASSETS_WRITE
from cms.services.assistant.mcp_client import MODE_COMPOSED_EDITOR
from cms.services.assistant_flag import assistant_enabled_for
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
    request: Request,
    db: AsyncSession,
) -> None:
    """Reject layouts that reference an asset the audience can't see.

    Two independent checks are applied to every asset declared by any
    widget in the layout:

    1. **Caller visibility.** The current user must be able to see each
       referenced asset (global, shared with one of their groups, or
       their own unshared upload). A referenced asset the caller cannot
       see is reported indistinguishably from a missing one so we don't
       disclose the existence of another user's private asset. This is
       what stops a personal composed slide from inlining someone
       else's private asset by guessing its UUID.

    2. **Audience widening.** Mirrors
       :func:`cms.routers.assets._validate_slideshow_acl` for the
       composed case — a referenced asset must be visible to a superset
       of the composed slide's *delivery* audience:

       * Composed slide is global → every referenced asset must also be
         global.
       * Composed slide is group-scoped → every referenced asset must be
         global, or shared with a superset of the composed slide's group
         set.
       * Composed slide is personal (no groups, not global) → no extra
         audience-widening check (it isn't delivered to a wider audience
         than the author).
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

    # Caller-visibility gate: scope the existence query to the assets the
    # current user can actually see. A referenced asset that exists but
    # isn't visible to the caller then falls into ``missing`` below and is
    # reported as "not found" — closing the cross-user private-asset leak
    # without disclosing that the asset exists.
    from cms.routers.assets import _visible_asset_ids  # noqa: WPS433

    user = getattr(request.state, "user", None)
    visible = await _visible_asset_ids(user, db) if user is not None else None

    asset_query = select(Asset).where(
        Asset.id.in_(referenced), Asset.deleted_at.is_(None),
    )
    if visible is not None:
        asset_query = asset_query.where(Asset.id.in_(visible))

    # Load each referenced asset + the groups it's shared with.
    asset_rows = (await db.execute(asset_query)).scalars().all()
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
        # Personal slide — caller-visibility was enforced by the gate
        # above; no audience-widening check needed (it isn't delivered to
        # a wider audience than the author).
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


def _build_widget_types() -> list[dict[str, Any]]:
    """Introspect the widget registry into LLM-friendly type descriptors.

    Each entry carries the widget's config JSON-schema, defaults, the
    required field names, and a ``references_asset`` flag so an AI
    caller knows which widgets need a real ``asset_id`` from the asset
    library. This is the source of truth the assistant consults before
    placing widgets, preventing hallucinated types / config fields.
    """
    import cms.composed.widgets  # noqa: F401, WPS433

    registry = get_registry()
    out: list[dict[str, Any]] = []
    for widget in registry.all():
        if getattr(widget, "assistant_hidden", False):
            # Legacy/internal widgets (e.g. the image-only widget) are
            # never offered to the assistant — it must use "media".
            continue
        schema = widget.ConfigSchema.model_json_schema()
        props = schema.get("properties", {})
        out.append(
            {
                "type": widget.slug,
                "display_name": widget.display_name,
                "icon": widget.icon,
                "config_version": widget.config_version,
                "references_asset": "asset_id" in props,
                "required_fields": schema.get("required", []),
                "default_config": widget.default_config(),
                "config_schema": schema,
            }
        )
    return out


def _friendly_to_layout_dict(
    widgets_in: Any,
    background_color: str | None,
    current_layout: dict[str, Any] | None,
) -> dict[str, Any]:
    """Translate the friendly widget list into a canonical Layout dict.

    The friendly shape hides the parts of the canonical ``Layout`` that
    are either locked (canvas 1920x1080, grid 12x8) or machine-managed
    (per-widget UUID ``id``, ``config_version``). Each friendly widget
    is ``{type, row, col, rowspan?, colspan?, config?, id?}``. A missing
    ``id`` gets a fresh ``uuid4``; a supplied ``id`` is preserved so
    edits keep widget identity. ``config_version`` is taken from the
    registered widget when the type is known.

    The returned dict is fed straight to ``Layout.model_validate`` so
    all shape/bounds errors surface through the same 422 path as the
    canonical PATCH endpoint. Raises 422 only for structurally
    impossible input (non-list widgets, non-object entries, malformed
    ``id``).
    """
    import cms.composed.widgets  # noqa: F401, WPS433

    registry = get_registry()

    if not isinstance(widgets_in, list):
        raise HTTPException(
            status_code=422,
            detail={
                "error": "invalid_widgets",
                "message": "widgets must be a list",
            },
        )

    widgets_out: list[dict[str, Any]] = []
    for idx, w in enumerate(widgets_in):
        if not isinstance(w, dict):
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "invalid_widget",
                    "message": f"widget at index {idx} must be an object",
                },
            )
        raw_id = w.get("id")
        if raw_id is not None:
            try:
                widget_id = str(uuid.UUID(str(raw_id)))
            except ValueError as exc:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "error": "invalid_widget_id",
                        "message": (
                            f"widget at index {idx} has a malformed id: "
                            f"{raw_id!r}"
                        ),
                    },
                ) from exc
        else:
            widget_id = str(uuid.uuid4())

        wtype = w.get("type")
        known = registry.get(wtype) if isinstance(wtype, str) else None
        config_version = known.config_version if known is not None else 1

        widgets_out.append(
            {
                "id": widget_id,
                "type": wtype,
                "cell": {
                    "row": w.get("row"),
                    "col": w.get("col"),
                    "rowspan": w.get("rowspan", 1),
                    "colspan": w.get("colspan", 1),
                },
                "config": w.get("config", {}),
                "config_version": config_version,
                "frame": w.get("frame"),
            }
        )

    if background_color is not None:
        bg_color = background_color
    elif current_layout:
        bg_color = (current_layout.get("background") or {}).get(
            "color", "#000000"
        )
    else:
        bg_color = "#000000"

    return {
        "background": {"color": bg_color},
        "widgets": widgets_out,
    }


async def _validate_persist_layout(
    asset: Asset,
    composed: ComposedSlide,
    layout: Layout,
    user: User,
    request: Request,
    db: AsyncSession,
    *,
    action: str,
) -> dict[str, Any]:
    """Run semantic + ACL validation, then persist the layout as a draft.

    Shared by the canonical PATCH endpoint and the friendly PUT endpoint
    so the validate -> ACL -> persist -> audit sequence lives in one
    place. Raises 422 (semantic) / 400 (ACL) exactly as the PATCH path.
    """
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

    await _check_referenced_asset_acl(layout, asset, request, db)

    composed.layout_json = layout.model_dump(mode="json")
    composed.schema_version = layout.schema_version
    composed.is_draft = True

    await audit_log(
        db,
        user=user,
        action=action,
        resource_type="asset",
        resource_id=str(asset.id),
        description=f"Updated composed-slide layout for '{asset.filename}'",
        details={"widget_count": len(layout.widgets)},
        request=request,
    )
    await db.commit()

    # Best-effort: queue a fresh snapshot thumbnail render for the grid.
    # Snapshots are generated on every save (drafts included) so the grid
    # always reflects the latest layout. A transient enqueue failure must
    # never block a save — the startup backfill repairs any miss.
    try:
        from cms.services.transcoder import enqueue_composed_thumbnail

        await enqueue_composed_thumbnail(asset, db)
    except Exception:  # noqa: BLE001
        logger.warning(
            "Failed to enqueue snapshot thumbnail for composed asset %s",
            asset.id, exc_info=True,
        )

    return {
        "id": str(asset.id),
        "is_draft": True,
        "widget_count": len(layout.widgets),
    }


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

    # Per-asset visibility gate for the slide itself and every asset it
    # references — a viewer who can see the slide must not be able to
    # inline an asset they can't see. Enforced inside build_composed_html
    # via this callback so the worker (trusted infra) can render the same
    # HTML without ACL checks.
    from cms.routers.assets import _verify_asset_access  # noqa: WPS433

    async def _verify(aid: uuid.UUID) -> None:
        await _verify_asset_access(aid, request, db)

    rendered = await build_composed_html(
        db, settings, asset_id, verify_asset=_verify,
    )

    # The weather widget is the only widget that makes a runtime network
    # call (a keyless Open-Meteo forecast fetch). The locked-down preview
    # CSP (default-src 'none', no connect-src) blocks that fetch, so the
    # preview would show the widget's offline "Weather unavailable"
    # fallback instead of live values. Allow exactly that one origin —
    # and only when the slide actually contains a weather widget, so a
    # plain text/image slide keeps the fully-locked CSP.
    csp = _PREVIEW_CSP
    if rendered.has_weather:
        csp = csp + "; connect-src https://api.open-meteo.com"

    headers = {
        "Content-Security-Policy": csp,
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "no-store",
    }
    return HTMLResponse(content=rendered.html_bytes, headers=headers)


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
    2. Runs ``validate_layout`` (semantic: bounds, unknown
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

    return await _validate_persist_layout(
        asset,
        composed,
        layout,
        user,
        request,
        db,
        action="asset.update_composed_layout",
    )


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


@router.get("/widget-types")
async def list_composed_widget_types() -> dict[str, Any]:
    """Return the catalog of available composed-slide widget types.

    Read-only registry introspection. Each entry has the widget's config
    JSON-schema, default config, required fields, and whether it
    references an asset. Powers the AI assistant's widget discovery so
    it never invents a widget type or config field.
    """
    return {"widget_types": _build_widget_types()}


@router.get("/{asset_id}/layout")
async def get_composed_layout(
    asset_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Return the current draft layout in the friendly widget shape.

    Read-only. Widget ``id``s are included so an AI edit can preserve
    widget identity by sending them back unchanged. The locked canvas
    and grid are reported for context but are not editable.
    """
    asset, composed = await _load_composed_for_write(asset_id, request, db)
    raw = composed.layout_json or {}
    bg = (raw.get("background") or {}).get("color", "#000000")

    widgets_out: list[dict[str, Any]] = []
    for w in raw.get("widgets", []):
        cell = w.get("cell") or {}
        widgets_out.append(
            {
                "id": w.get("id"),
                "type": w.get("type"),
                "row": cell.get("row"),
                "col": cell.get("col"),
                "rowspan": cell.get("rowspan", 1),
                "colspan": cell.get("colspan", 1),
                "config": w.get("config", {}),
                "config_version": w.get("config_version", 1),
                "frame": w.get("frame"),
            }
        )

    return {
        "id": str(asset.id),
        "is_draft": composed.is_draft,
        "background_color": bg,
        "canvas": {"width": CANVAS_WIDTH, "height": CANVAS_HEIGHT},
        "grid": {"rows": GRID_ROWS, "cols": GRID_COLS},
        "widgets": widgets_out,
    }


@router.put("/{asset_id}/layout-friendly")
async def put_composed_layout_friendly(
    asset_id: uuid.UUID,
    request: Request,
    body: dict[str, Any] = Body(...),
    user: User = Depends(require_permission(ASSETS_WRITE)),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Replace a draft's layout from the friendly widget shape.

    Body: ``{widgets: [...], background_color?: "#rrggbb"}`` where each
    widget is ``{type, row, col, rowspan?, colspan?, config?, id?}``.
    The server assigns/preserves widget UUIDs and pins the locked
    canvas/grid, then runs the exact same shape + semantic + asset-ACL
    validation as the canonical PATCH endpoint. Sets ``is_draft=True``.

    This is the AI-assistant write path: the LLM never has to spell out
    the locked canvas/grid or manage widget UUIDs, which is the source
    of the "extra inputs not permitted" failures it would otherwise hit.
    """
    asset, composed = await _load_composed_for_write(asset_id, request, db)

    # Trigger widget registration before validate_layout uses the registry.
    import cms.composed.widgets  # noqa: F401, WPS433

    layout_dict = _friendly_to_layout_dict(
        body.get("widgets", []),
        body.get("background_color"),
        composed.layout_json,
    )
    try:
        layout = Layout.model_validate(layout_dict)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            status_code=422,
            detail={"error": "invalid_layout_shape", "message": str(e)},
        ) from e

    return await _validate_persist_layout(
        asset,
        composed,
        layout,
        user,
        request,
        db,
        action="asset.update_composed_layout",
    )


@router.post("/{asset_id}/assistant/thread")
async def composed_assistant_thread(
    asset_id: uuid.UUID,
    request: Request,
    user: User = Depends(require_permission(ASSETS_WRITE)),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Get-or-create the editor-scoped AI chat thread for this slide.

    Powers the embedded chat panel in the composed-slide editor. The
    thread is bound to ``asset_id`` and runs in ``composed_editor`` mode,
    which scopes the assistant to the composed read/draft-write tools and
    forces every composed tool call onto *this* slide.

    Reuses the caller's newest existing ``composed_editor`` thread for the
    slide if one exists (so reopening the editor resumes the same
    conversation); otherwise creates one. Returns ``{thread_id, created}``.

    Gated identically to the editor itself: requires ``ASSETS_WRITE``,
    that the slide exists and is visible to the caller, and that the
    Assistant feature is enabled for the caller (404 otherwise, to keep
    the hidden feature invisible).
    """
    asset, _composed = await _load_composed_for_write(asset_id, request, db)

    if not await assistant_enabled_for(db, user):
        raise HTTPException(status_code=404, detail="Not found")

    existing = (
        await db.execute(
            select(ChatThread)
            .where(
                ChatThread.user_id == user.id,
                ChatThread.composed_asset_id == asset_id,
                ChatThread.mode == MODE_COMPOSED_EDITOR,
            )
            .order_by(ChatThread.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return {"thread_id": str(existing.id), "created": False}

    asset_name = (
        asset.display_name or asset.original_filename or asset.filename or "slide"
    )
    thread = ChatThread(
        user_id=user.id,
        mode=MODE_COMPOSED_EDITOR,
        composed_asset_id=asset_id,
        title=f"Editing {asset_name}"[:200],
    )
    db.add(thread)
    await db.commit()
    await db.refresh(thread)
    return {"thread_id": str(thread.id), "created": True}
