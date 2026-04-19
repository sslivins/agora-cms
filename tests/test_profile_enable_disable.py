"""Tests for profile enable/disable (issue #237).

When a profile is disabled:
  * new asset uploads / new profile creation must NOT enqueue variants
    for that profile
  * existing READY variants stay intact (re-enable is instant)
  * in-flight / pending transcode jobs for that profile get cancelled

When it's re-enabled:
  * fan-out re-runs, so any assets uploaded while it was off now get variants
"""

import uuid

import pytest
from sqlalchemy import select


@pytest.mark.asyncio
class TestProfileEnableDisableApi:
    """API surface: POST /enable and /disable endpoints."""

    async def test_default_enabled_true(self, client, db_session):
        from cms.models.device_profile import DeviceProfile

        profile = DeviceProfile(name="new-default", video_codec="h264")
        db_session.add(profile)
        await db_session.commit()
        await db_session.refresh(profile)
        assert profile.enabled is True

    async def test_disable_then_enable_roundtrip(self, client, db_session):
        from cms.models.device_profile import DeviceProfile

        profile = DeviceProfile(name="toggle-me", video_codec="h264")
        db_session.add(profile)
        await db_session.commit()
        pid = profile.id

        r = await client.post(f"/api/profiles/{pid}/disable")
        assert r.status_code == 200
        assert r.json()["enabled"] is False

        db_session.expunge_all()
        got = await db_session.get(DeviceProfile, pid)
        assert got.enabled is False

        r = await client.post(f"/api/profiles/{pid}/enable")
        assert r.status_code == 200
        assert r.json()["enabled"] is True

        db_session.expunge_all()
        got = await db_session.get(DeviceProfile, pid)
        assert got.enabled is True

    async def test_disable_is_idempotent(self, client, db_session):
        from cms.models.device_profile import DeviceProfile

        profile = DeviceProfile(name="idempotent", video_codec="h264", enabled=False)
        db_session.add(profile)
        await db_session.commit()

        r = await client.post(f"/api/profiles/{profile.id}/disable")
        assert r.status_code == 200
        assert r.json()["enabled"] is False

    async def test_enable_is_idempotent(self, client, db_session):
        from cms.models.device_profile import DeviceProfile

        profile = DeviceProfile(name="idempotent-en", video_codec="h264")
        db_session.add(profile)
        await db_session.commit()

        r = await client.post(f"/api/profiles/{profile.id}/enable")
        assert r.status_code == 200
        assert r.json()["enabled"] is True

    async def test_enable_disable_404(self, client):
        fake = uuid.uuid4()
        r = await client.post(f"/api/profiles/{fake}/enable")
        assert r.status_code == 404
        r = await client.post(f"/api/profiles/{fake}/disable")
        assert r.status_code == 404

    async def test_profile_out_includes_enabled_field(self, client, db_session):
        from cms.models.device_profile import DeviceProfile

        profile = DeviceProfile(name="field-check", video_codec="h264")
        db_session.add(profile)
        await db_session.commit()

        r = await client.get("/api/profiles")
        assert r.status_code == 200
        body = r.json()
        entry = next(p for p in body if p["id"] == str(profile.id))
        assert entry["enabled"] is True


@pytest.mark.asyncio
class TestDisabledProfileSkipsTranscode:
    """Transcoder helpers must skip disabled profiles."""

    async def test_enqueue_for_new_profile_skips_disabled(self, client, db_session):
        """Profile created disabled: fan-out returns no variants even with
        existing video assets in the library."""
        from cms.models.asset import Asset, AssetType, AssetVariant
        from cms.models.device_profile import DeviceProfile
        from cms.services.transcoder import enqueue_for_new_profile

        asset = Asset(
            filename="existing.mp4", asset_type=AssetType.VIDEO,
            size_bytes=1000, checksum=f"skip-{uuid.uuid4().hex[:8]}",
        )
        db_session.add(asset)
        await db_session.flush()

        profile = DeviceProfile(
            name="disabled-at-create", video_codec="h264", enabled=False,
        )
        db_session.add(profile)
        await db_session.commit()

        ids = await enqueue_for_new_profile(profile.id, db_session)
        assert ids == []

        db_session.expunge_all()
        result = await db_session.execute(
            select(AssetVariant).where(AssetVariant.profile_id == profile.id)
        )
        assert result.scalars().all() == []

    async def test_asset_upload_skips_disabled_profile(self, client, db_session):
        """A disabled profile must NOT get a variant when a new asset is
        created via the transcoder fan-out path."""
        from cms.models.asset import Asset, AssetType, AssetVariant
        from cms.models.device_profile import DeviceProfile
        from cms.services.transcoder import _enqueue_transcoding_for_asset

        on_profile = DeviceProfile(
            name="on-upload", video_codec="h264", enabled=True,
        )
        off_profile = DeviceProfile(
            name="off-upload", video_codec="h264", enabled=False,
        )
        db_session.add_all([on_profile, off_profile])
        await db_session.flush()

        asset = Asset(
            filename="fresh.mp4", asset_type=AssetType.VIDEO,
            size_bytes=1000, checksum=f"upload-{uuid.uuid4().hex[:8]}",
        )
        db_session.add(asset)
        await db_session.commit()

        await _enqueue_transcoding_for_asset(asset, db_session)

        db_session.expunge_all()
        on_variants = (await db_session.execute(
            select(AssetVariant).where(AssetVariant.profile_id == on_profile.id)
        )).scalars().all()
        off_variants = (await db_session.execute(
            select(AssetVariant).where(AssetVariant.profile_id == off_profile.id)
        )).scalars().all()

        assert len(on_variants) == 1, "enabled profile should get a variant"
        assert off_variants == [], "disabled profile must not get a variant"

    async def test_reenable_fanout_picks_up_missed_assets(
        self, client, db_session, tmp_path, monkeypatch,
    ):
        """After re-enabling, fan-out creates variants for assets that were
        uploaded while the profile was disabled."""
        from cms.models.asset import Asset, AssetType, AssetVariant
        from cms.models.device_profile import DeviceProfile

        profile = DeviceProfile(
            name="reenable-fanout", video_codec="h264", enabled=False,
        )
        db_session.add(profile)
        await db_session.flush()

        asset = Asset(
            filename="missed.mp4", asset_type=AssetType.VIDEO,
            size_bytes=2000, checksum=f"reen-{uuid.uuid4().hex[:8]}",
        )
        db_session.add(asset)
        await db_session.commit()
        await db_session.close()

        r = await client.post(f"/api/profiles/{profile.id}/enable")
        assert r.status_code == 200
        assert r.json()["enabled"] is True

        result = await db_session.execute(
            select(AssetVariant).where(
                AssetVariant.profile_id == profile.id,
                AssetVariant.source_asset_id == asset.id,
            )
        )
        variants = result.scalars().all()
        assert len(variants) == 1, (
            f"re-enable should enqueue a variant for the missed asset, got {variants}"
        )
