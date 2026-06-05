"""Server-side gate: a composed slide can't be scheduled until it's published.

The user-facing bug this guards against: a composed slide was saved + scheduled
but never **published**, so no HTML bundle/checksum existed and the device 404'd
forever trying to download it. ``require_asset_ready`` now rejects an
unpublished composed asset (no checksum) with 422, while leaving a
published-then-edited (stale-but-published) slide schedulable because its old
bundle/checksum still serves the device.

These are DB-integration tests against the real ``require_asset_ready``.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from cms.models.asset import Asset, AssetType
from cms.services.asset_readiness import require_asset_ready


@pytest.mark.asyncio
async def test_unpublished_composed_is_rejected(db_session):
    """COMPOSED asset with no checksum (never published) → 422."""
    asset = Asset(
        filename="New Slide",
        asset_type=AssetType.COMPOSED,
        checksum=None,
    )
    db_session.add(asset)
    await db_session.flush()

    with pytest.raises(HTTPException) as ei:
        await require_asset_ready(db_session, asset.id)
    assert ei.value.status_code == 422
    assert "published" in str(ei.value.detail).lower()


@pytest.mark.asyncio
async def test_published_composed_is_allowed(db_session):
    """COMPOSED asset with a bundle checksum → passes the gate."""
    asset = Asset(
        filename="composed-deadbeef.html",
        asset_type=AssetType.COMPOSED,
        checksum="abc123def456",
        size_bytes=2048,
    )
    db_session.add(asset)
    await db_session.flush()

    out = await require_asset_ready(db_session, asset.id)
    assert out.id == asset.id


@pytest.mark.asyncio
async def test_stale_but_published_composed_is_allowed(db_session):
    """Published-then-edited slide: checksum still present from the last
    published bundle → must stay schedulable (device plays last version)."""
    asset = Asset(
        filename="composed-cafe.html",
        asset_type=AssetType.COMPOSED,
        checksum="oldbundlechecksum",
        size_bytes=4096,
    )
    db_session.add(asset)
    await db_session.flush()

    out = await require_asset_ready(db_session, asset.id)
    assert out.id == asset.id


@pytest.mark.asyncio
async def test_non_composed_without_variants_is_allowed(db_session):
    """A plain URL-style asset (no variants, not composed) is unaffected."""
    asset = Asset(
        filename="https://example.com",
        asset_type=AssetType.WEBPAGE,
        url="https://example.com",
    )
    db_session.add(asset)
    await db_session.flush()

    out = await require_asset_ready(db_session, asset.id)
    assert out.id == asset.id


def test_asset_out_marks_unpublished_composed():
    """Library serializer flags a never-published composed slide so the
    'UNPUBLISHED' badge renders in the asset grid/table."""
    import uuid as _uuid
    from datetime import datetime, timezone
    from cms.routers.assets import _asset_out_with_thumb

    asset = Asset(
        id=_uuid.uuid4(),
        filename="New Slide",
        asset_type=AssetType.COMPOSED,
        checksum="",
        size_bytes=0,
        uploaded_at=datetime.now(timezone.utc),
        is_global=False,
    )
    out = _asset_out_with_thumb(asset, {})
    assert out.unpublished is True


def test_asset_out_published_composed_not_flagged():
    """A published composed slide (has a bundle checksum) is not flagged."""
    import uuid as _uuid
    from datetime import datetime, timezone
    from cms.routers.assets import _asset_out_with_thumb

    asset = Asset(
        id=_uuid.uuid4(),
        filename="composed-deadbeef.html",
        asset_type=AssetType.COMPOSED,
        checksum="abc123",
        size_bytes=2048,
        uploaded_at=datetime.now(timezone.utc),
        is_global=False,
    )
    out = _asset_out_with_thumb(asset, {})
    assert out.unpublished is False


def test_asset_out_non_composed_not_flagged():
    """Non-composed assets never carry the unpublished badge."""
    import uuid as _uuid
    from datetime import datetime, timezone
    from cms.routers.assets import _asset_out_with_thumb

    asset = Asset(
        id=_uuid.uuid4(),
        filename="clip.mp4",
        asset_type=AssetType.VIDEO,
        checksum="",
        size_bytes=1024,
        uploaded_at=datetime.now(timezone.utc),
        is_global=False,
    )
    out = _asset_out_with_thumb(asset, {})
    assert out.unpublished is False
