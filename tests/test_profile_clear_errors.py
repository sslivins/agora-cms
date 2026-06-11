"""Tests for the POST /api/profiles/clear-errors endpoint.

The Transcoding Queue panel surfaces FAILED variants (permanent,
non-retryable transcode failures) that never transition out on their own.
This endpoint lets a ``profiles:write`` holder dismiss them: it deletes the
FAILED variant rows (best-effort output-file cleanup) and removes any
matching transcode-failure notifications (one-way tie).
"""

import uuid

import pytest
from sqlalchemy import select


def _mk_asset_variant(status):
    from cms.models.asset import Asset, AssetType, AssetVariant, VariantStatus
    from cms.models.device_profile import DeviceProfile

    asset = Asset(
        filename=f"{uuid.uuid4().hex}.mp4", asset_type=AssetType.VIDEO,
        size_bytes=1000, checksum=uuid.uuid4().hex[:8],
    )
    profile = DeviceProfile(name=f"clr-{uuid.uuid4().hex[:8]}", video_codec="h264")
    variant = AssetVariant(
        filename=f"{uuid.uuid4()}.mp4", status=status,
    )
    return asset, profile, variant


@pytest.mark.asyncio
class TestClearTranscodeErrors:
    async def test_clears_failed_variants(self, client, db_session):
        from cms.models.asset import VariantStatus

        asset, profile, variant = _mk_asset_variant(VariantStatus.FAILED)
        db_session.add_all([asset, profile])
        await db_session.flush()
        variant.source_asset_id = asset.id
        variant.profile_id = profile.id
        variant.error_message = "ffmpeg exploded"
        db_session.add(variant)
        await db_session.commit()
        variant_id = variant.id

        resp = await client.post("/api/profiles/clear-errors")
        assert resp.status_code == 200, resp.text
        assert resp.json()["cleared"] == 1

        db_session.expunge_all()
        from cms.models.asset import AssetVariant
        gone = (await db_session.execute(
            select(AssetVariant).where(AssetVariant.id == variant_id)
        )).scalar_one_or_none()
        assert gone is None

    async def test_does_not_touch_non_failed_variants(self, client, db_session):
        from cms.models.asset import AssetVariant, VariantStatus

        asset, profile, _ = _mk_asset_variant(VariantStatus.READY)
        db_session.add_all([asset, profile])
        await db_session.flush()
        ready = AssetVariant(
            source_asset_id=asset.id, profile_id=profile.id,
            filename=f"{uuid.uuid4()}.mp4", status=VariantStatus.READY,
        )
        pending = AssetVariant(
            source_asset_id=asset.id, profile_id=profile.id,
            filename=f"{uuid.uuid4()}.mp4", status=VariantStatus.PENDING,
        )
        db_session.add_all([ready, pending])
        await db_session.commit()
        ready_id, pending_id = ready.id, pending.id

        resp = await client.post("/api/profiles/clear-errors")
        assert resp.status_code == 200
        assert resp.json()["cleared"] == 0

        db_session.expunge_all()
        remaining = (await db_session.execute(
            select(AssetVariant.id).where(
                AssetVariant.id.in_([ready_id, pending_id])
            )
        )).scalars().all()
        assert set(remaining) == {ready_id, pending_id}

    async def test_deletes_matching_notifications_one_way_tie(self, client, db_session):
        from cms.models.asset import VariantStatus
        from cms.models.notification import Notification
        from cms.services.transcoder import TRANSCODE_FAIL_NOTIFICATION_TITLE

        asset, profile, variant = _mk_asset_variant(VariantStatus.FAILED)
        db_session.add_all([asset, profile])
        await db_session.flush()
        variant.source_asset_id = asset.id
        variant.profile_id = profile.id
        db_session.add(variant)
        await db_session.flush()

        matching = Notification(
            scope="system", level="error",
            title=TRANSCODE_FAIL_NOTIFICATION_TITLE,
            message="boom",
            details={"variant_id": str(variant.id), "asset_id": str(asset.id)},
        )
        # An unrelated notification (different variant) must survive.
        unrelated = Notification(
            scope="system", level="error",
            title=TRANSCODE_FAIL_NOTIFICATION_TITLE,
            message="other",
            details={"variant_id": str(uuid.uuid4())},
        )
        db_session.add_all([matching, unrelated])
        await db_session.commit()
        unrelated_id = unrelated.id

        resp = await client.post("/api/profiles/clear-errors")
        assert resp.status_code == 200
        body = resp.json()
        assert body["cleared"] == 1
        assert body["notifications_cleared"] == 1

        db_session.expunge_all()
        survivors = (await db_session.execute(select(Notification.id))).scalars().all()
        assert unrelated_id in survivors

    async def test_requires_profiles_write(self, operator_client, db_session):
        """Operator holds profiles:read but NOT profiles:write → 403."""
        from cms.models.asset import VariantStatus

        asset, profile, variant = _mk_asset_variant(VariantStatus.FAILED)
        db_session.add_all([asset, profile])
        await db_session.flush()
        variant.source_asset_id = asset.id
        variant.profile_id = profile.id
        db_session.add(variant)
        await db_session.commit()

        resp = await operator_client.post("/api/profiles/clear-errors")
        assert resp.status_code == 403, resp.text
