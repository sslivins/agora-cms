"""Deleted assets must not linger in the Transcoding Queue.

Deleting an asset is a soft delete, and its variant rows survive until the
worker marks the jobs terminal and the reaper clears them. The queue card
read variants without excluding deleted assets, so work for assets the user
had already deleted kept showing -- with no way to clear it from the UI,
since the asset was gone from the library.

Admins saw it worst: _visible_asset_ids returns None for them, so nothing
else narrowed the query.
"""
import pytest

pytestmark = pytest.mark.asyncio


async def _asset_with_pending_variant(db_session, filename="queued.mp4"):
    from cms.models.asset import Asset, AssetType, AssetVariant, VariantStatus
    from cms.models.device_profile import DeviceProfile
    from sqlalchemy import select

    profile = (
        await db_session.execute(select(DeviceProfile).limit(1))
    ).scalar_one_or_none()
    if profile is None:
        profile = DeviceProfile(name="queue-test-profile")
        db_session.add(profile)
        await db_session.flush()

    asset = Asset(
        filename=filename,
        asset_type=AssetType.VIDEO,
        size_bytes=1234,
        checksum=filename,
    )
    db_session.add(asset)
    await db_session.flush()

    variant = AssetVariant(
        source_asset_id=asset.id,
        profile_id=profile.id,
        filename=f"variant-{filename}",
        status=VariantStatus.PENDING,
    )
    db_session.add(variant)
    await db_session.commit()
    return asset, variant, profile


class TestQueueExcludesDeletedAssets:
    async def test_pending_variant_is_listed_while_the_asset_lives(
        self, client, db_session
    ):
        """Precondition: without this the next test could pass vacuously."""
        asset, variant, _ = await _asset_with_pending_variant(
            db_session, "queue-live.mp4"
        )

        data = (await client.get("/api/profiles/status")).json()

        assert str(variant.id) in {q["id"] for q in data["queue"]}
        assert data["queue_count"] >= 1

    async def test_pending_variant_disappears_once_the_asset_is_deleted(
        self, client, db_session
    ):
        asset, variant, _ = await _asset_with_pending_variant(
            db_session, "queue-deleted.mp4"
        )

        resp = await client.delete(f"/api/assets/{asset.id}")
        assert resp.status_code in (200, 204)

        data = (await client.get("/api/profiles/status")).json()

        assert str(variant.id) not in {q["id"] for q in data["queue"]}

    async def test_profile_variant_totals_ignore_deleted_assets(
        self, client, db_session
    ):
        """Otherwise the denominator counts work that will never be done and
        a profile can never read as fully transcoded."""
        asset, _variant, profile = await _asset_with_pending_variant(
            db_session, "queue-totals.mp4"
        )

        before = (await client.get("/api/profiles/status")).json()
        total_before = next(
            p["total_variants"] for p in before["profiles"] if p["id"] == str(profile.id)
        )

        resp = await client.delete(f"/api/assets/{asset.id}")
        assert resp.status_code in (200, 204)

        after = (await client.get("/api/profiles/status")).json()
        total_after = next(
            p["total_variants"] for p in after["profiles"] if p["id"] == str(profile.id)
        )

        assert total_after == total_before - 1


class TestQueueExcludesSupersededVariants:
    """The asset is alive here -- only the variant is retired.

    Editing a profile supersedes its variants: the old rows are left in
    place on purpose so devices keep playing the last good blob, and the
    reaper soft-deletes them once the replacement is READY. A variant
    cancelled mid-flight keeps its PENDING status, so a routine profile
    edit could strand rows in the queue card with no asset deletion
    involved at all.
    """

    async def test_soft_deleted_variant_leaves_the_queue(self, client, db_session):
        from datetime import datetime, timezone

        _asset, variant, _profile = await _asset_with_pending_variant(
            db_session, "queue-superseded.mp4"
        )

        assert str(variant.id) in {
            q["id"] for q in (await client.get("/api/profiles/status")).json()["queue"]
        }

        variant.deleted_at = datetime.now(timezone.utc)
        await db_session.commit()

        data = (await client.get("/api/profiles/status")).json()
        assert str(variant.id) not in {q["id"] for q in data["queue"]}

    async def test_soft_deleted_variant_leaves_the_profile_totals(
        self, client, db_session
    ):
        from datetime import datetime, timezone

        _asset, variant, profile = await _asset_with_pending_variant(
            db_session, "queue-superseded-totals.mp4"
        )

        before = (await client.get("/api/profiles/status")).json()
        total_before = next(
            p["total_variants"] for p in before["profiles"] if p["id"] == str(profile.id)
        )

        variant.deleted_at = datetime.now(timezone.utc)
        await db_session.commit()

        after = (await client.get("/api/profiles/status")).json()
        total_after = next(
            p["total_variants"] for p in after["profiles"] if p["id"] == str(profile.id)
        )

        assert total_after == total_before - 1
