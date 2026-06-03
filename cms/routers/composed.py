"""Phase 1A: live preview endpoint for composed slides.

Renders the composed slide's current ``layout_json`` to a
self-contained HTML document via the same bundle builder used at
publish time, and serves it inline. No bundle is written to disk;
this is the editor's "live preview" path.

CSP is locked down: only the inline content the bundle builder
generates is allowed; no external scripts, styles, fonts, or
images. Frame ancestors are restricted to the CMS origin so the
preview can render inside an editor ``<iframe>`` but cannot be
embedded elsewhere.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cms.auth import require_auth
from cms.composed.bundle import BundleValidationError, build_bundle
from cms.composed.registry import get_registry
from cms.composed.schema import Layout
from cms.database import get_db
from cms.models.asset import Asset, AssetType
from cms.models.composed_slide import ComposedSlide

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
