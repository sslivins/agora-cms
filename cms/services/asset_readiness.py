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
    return asset
