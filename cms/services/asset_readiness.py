"""Asset-readiness gate for splash-screen and schedule assignments (issue #201).

Centralises the "can this asset be assigned to a device / group splash or
scheduled for playback?" check so routers (devices / schedules) all apply
the same rule.

The rule itself lives in :func:`cms.services.variant_view.is_asset_ready`.
This module adds a small async wrapper that loads the asset + its variants
from the DB and raises an HTTP 422 when the gate fails.
"""

from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from cms.models.asset import Asset
from cms.services.variant_view import is_asset_ready
from shared.models.asset import AssetType

# Reason string surfaced when a composed slide has never been published
# (no rendered bundle / checksum yet). Kept as a module constant so the UI
# annotation (scheduler dropdown) and the server-side gate share one phrase.
COMPOSED_UNPUBLISHED_REASON = "not published yet"


def composed_unpublished_reason(asset: Asset | None) -> str | None:
    """Return a reason when ``asset`` is a composed slide with no bundle yet.

    A composed slide only becomes device-deliverable after **Publish**, which
    renders the HTML bundle and stamps the bound Asset's ``checksum`` /
    ``filename``. Until then there is no blob for the device to download, so
    scheduling it 404s the device in a retry loop.

    The check intentionally gates on the **checksum** (i.e. "has a published
    bundle"), not on ``ComposedSlide.is_draft``. A slide that was published and
    then edited is flagged ``is_draft=True`` but still has a valid checksum and
    bundle, so the device can keep playing the last published version — that
    case stays schedulable. Only *never-published* slides are blocked here.

    Returns the reason string, or ``None`` when the asset is fine to schedule.
    """
    if asset is None:
        return None
    if getattr(asset, "asset_type", None) != AssetType.COMPOSED:
        return None
    return COMPOSED_UNPUBLISHED_REASON if not getattr(asset, "checksum", None) else None


async def require_asset_ready(db: AsyncSession, asset_id: uuid.UUID) -> Asset:
    """Load the asset + variants and raise 422 if it isn't ready.

    Returns the loaded Asset on success so callers don't have to re-fetch.

    Raises:
        HTTPException(404): asset not found.
        HTTPException(422): asset exists but isn't ready for assignment —
            a variant is still PROCESSING/PENDING (``"transcoding…"``) or
            the only non-READY rows for some profile are FAILED
            (``"transcode failed"``).
    """
    result = await db.execute(
        select(Asset)
        .options(selectinload(Asset.variants))
        .where(Asset.id == asset_id)
    )
    asset = result.scalar_one_or_none()
    if asset is None:
        raise HTTPException(status_code=404, detail="Asset not found")

    ready, reason = is_asset_ready(asset.variants)
    if not ready:
        raise HTTPException(
            status_code=422,
            detail=f"Asset is not ready for assignment ({reason}).",
        )

    # Composed slides must be published before they can be scheduled — an
    # unpublished slide has no bundle for the device to download (404 loop).
    composed_reason = composed_unpublished_reason(asset)
    if composed_reason:
        raise HTTPException(
            status_code=422,
            detail="This composed slide hasn't been published yet. "
            "Open it in the editor and click Publish before scheduling it.",
        )
    return asset
