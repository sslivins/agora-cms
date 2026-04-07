"""Tests for re-transcoding variants when profile settings change."""

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select


@pytest.mark.asyncio
class TestProfileUpdateRetranscode:
    """Updating transcoding-relevant profile settings should reset variants."""

    async def test_changing_crf_resets_ready_variants(self, client, db_session):
        """Changing CRF should reset READY variants back to PENDING."""
        from cms.models.asset import Asset, AssetType, AssetVariant, VariantStatus
        from cms.models.device_profile import DeviceProfile

        profile = DeviceProfile(name="retrans-crf", video_codec="h264", crf=23)
        db_session.add(profile)
        await db_session.flush()

        asset = Asset(
            filename="vid1.mp4", asset_type=AssetType.VIDEO,
            size_bytes=1000, checksum="abc",
        )
        db_session.add(asset)
        await db_session.flush()

        variant = AssetVariant(
            source_asset_id=asset.id,
            profile_id=profile.id,
            filename=f"{uuid.uuid4()}.mp4",
            status=VariantStatus.READY,
            size_bytes=500,
        )
        db_session.add(variant)
        await db_session.commit()

        # Update CRF
        resp = await client.put(
            f"/api/profiles/{profile.id}",
            json={"crf": 18},
        )
        assert resp.status_code == 200

        # Variant should be reset to PENDING
        await db_session.refresh(variant)
        assert variant.status == VariantStatus.PENDING

    async def test_changing_description_does_not_retranscode(self, client, db_session):
        """Changing description should NOT reset variants."""
        from cms.models.asset import Asset, AssetType, AssetVariant, VariantStatus
        from cms.models.device_profile import DeviceProfile

        profile = DeviceProfile(name="retrans-desc", video_codec="h264")
        db_session.add(profile)
        await db_session.flush()

        asset = Asset(
            filename="vid2.mp4", asset_type=AssetType.VIDEO,
            size_bytes=1000, checksum="abc",
        )
        db_session.add(asset)
        await db_session.flush()

        variant = AssetVariant(
            source_asset_id=asset.id,
            profile_id=profile.id,
            filename=f"{uuid.uuid4()}.mp4",
            status=VariantStatus.READY,
            size_bytes=500,
        )
        db_session.add(variant)
        await db_session.commit()

        resp = await client.put(
            f"/api/profiles/{profile.id}",
            json={"description": "Updated description"},
        )
        assert resp.status_code == 200

        await db_session.refresh(variant)
        assert variant.status == VariantStatus.READY

    async def test_changing_resolution_resets_variants(self, client, db_session):
        """Changing max_width should reset READY variants."""
        from cms.models.asset import Asset, AssetType, AssetVariant, VariantStatus
        from cms.models.device_profile import DeviceProfile

        profile = DeviceProfile(name="retrans-res", video_codec="h264", max_width=1920)
        db_session.add(profile)
        await db_session.flush()

        asset = Asset(
            filename="vid3.mp4", asset_type=AssetType.VIDEO,
            size_bytes=1000, checksum="abc",
        )
        db_session.add(asset)
        await db_session.flush()

        variant = AssetVariant(
            source_asset_id=asset.id,
            profile_id=profile.id,
            filename=f"{uuid.uuid4()}.mp4",
            status=VariantStatus.READY,
            size_bytes=500,
        )
        db_session.add(variant)
        await db_session.commit()

        resp = await client.put(
            f"/api/profiles/{profile.id}",
            json={"max_width": 1280},
        )
        assert resp.status_code == 200

        await db_session.refresh(variant)
        assert variant.status == VariantStatus.PENDING

    async def test_retranscode_response_shows_zero_ready(self, client, db_session):
        """After a transcoding-relevant change, ready_variants should be 0."""
        from cms.models.asset import Asset, AssetType, AssetVariant, VariantStatus
        from cms.models.device_profile import DeviceProfile

        profile = DeviceProfile(name="retrans-resp", video_codec="h264", crf=23)
        db_session.add(profile)
        await db_session.flush()

        asset = Asset(
            filename="vid4.mp4", asset_type=AssetType.VIDEO,
            size_bytes=1000, checksum="abc",
        )
        db_session.add(asset)
        await db_session.flush()

        variant = AssetVariant(
            source_asset_id=asset.id,
            profile_id=profile.id,
            filename=f"{uuid.uuid4()}.mp4",
            status=VariantStatus.READY,
            size_bytes=500,
        )
        db_session.add(variant)
        await db_session.commit()

        resp = await client.put(
            f"/api/profiles/{profile.id}",
            json={"crf": 18},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_variants"] == 1
        assert data["ready_variants"] == 0
