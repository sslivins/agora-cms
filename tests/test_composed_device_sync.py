"""Phase 1A: confirm a published COMPOSED asset flows through
``_resolve_asset_for_device`` like any other single-file asset.

This is a no-edits-expected scheduler/device-sync verification — the
default branch in ``cms/services/device_inbound.py`` should handle
COMPOSED transparently because COMPOSED is not in the (VIDEO/IMAGE/
SAVED_STREAM) transcode set and is not SLIDESHOW.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cms.models.asset import Asset, AssetType
from cms.services.device_inbound import _resolve_asset_for_device


@pytest.mark.asyncio
async def test_composed_asset_resolves_via_default_file_path(db_session):
    """A COMPOSED asset must round-trip filename/checksum/size_bytes
    into a FetchAssetMessage identical to a regular file asset."""
    asset = Asset(
        filename="composed-deadbeef.html",
        asset_type=AssetType.COMPOSED,
        size_bytes=12345,
        checksum="abc123def456",
    )
    db_session.add(asset)
    await db_session.flush()

    device = MagicMock()
    device.profile_id = None

    fake_storage = AsyncMock()
    fake_storage.get_device_download_url = AsyncMock(
        return_value="https://cdn/composed-deadbeef.html"
    )

    with patch("cms.services.device_inbound.get_storage", return_value=fake_storage):
        fetch = await _resolve_asset_for_device(
            asset, device, "https://cms.example", db_session,
        )

    assert fetch is not None
    assert fetch.asset_name == "composed-deadbeef.html"
    assert fetch.checksum == "abc123def456"
    assert fetch.size_bytes == 12345
    assert fetch.asset_type == "composed"
    assert fetch.download_url == "https://cdn/composed-deadbeef.html"

    # Confirm storage was called with the asset.filename — NOT a
    # variant path. That's the contract for non-transcoded assets.
    fake_storage.get_device_download_url.assert_awaited_once()
    call_args = fake_storage.get_device_download_url.call_args
    assert call_args.args[0] == "composed-deadbeef.html"
