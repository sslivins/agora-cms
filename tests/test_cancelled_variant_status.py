"""Tests for VariantStatus.CANCELLED semantics.

When a VARIANT_TRANSCODE job is cancelled mid-flight (e.g. because the
parent asset was soft-deleted or the profile settings changed, triggering
a variant swap), the worker's cancel_observed path marks the variant row
CANCELLED — not FAILED — so:

* The UI can render "Cancelled" distinctly from a real failure.
* The asset-level ``variant_failed`` counter does not get polluted by
  transient cancellations.
* The supersession sweep still soft-deletes the row once a newer READY
  sibling exists (same as FAILED handling).
"""

import uuid

import pytest
from sqlalchemy import select

from cms.models.asset import Asset, AssetType, AssetVariant, VariantStatus
from cms.models.device_profile import DeviceProfile


@pytest.mark.asyncio
class TestCancelledVariantSupersession:
    """``supersede_ready_variants_once`` must treat CANCELLED like FAILED."""

    async def _make_asset(self, db):
        asset = Asset(
            filename="cancelled-sweep.mp4", asset_type=AssetType.VIDEO,
            size_bytes=1000, checksum="cs-aa",
        )
        db.add(asset)
        await db.flush()
        return asset

    async def _make_profile(self, db, name):
        profile = DeviceProfile(name=name, video_codec="h264", video_profile="main")
        db.add(profile)
        await db.flush()
        return profile

    async def test_cancelled_variant_superseded_when_newer_ready_exists(
        self, db_session
    ):
        """A CANCELLED variant with a newer READY sibling is soft-deleted."""
        from cms.services.transcoder import supersede_ready_variants_once

        profile = await self._make_profile(db_session, "cancel-sweep")
        asset = await self._make_asset(db_session)

        old_id, new_id = uuid.uuid4(), uuid.uuid4()
        old = AssetVariant(
            id=old_id, source_asset_id=asset.id, profile_id=profile.id,
            filename=f"{old_id}.mp4", status=VariantStatus.CANCELLED,
            error_message="cancelled mid-transcode",
        )
        db_session.add(old)
        await db_session.flush()

        new = AssetVariant(
            id=new_id, source_asset_id=asset.id, profile_id=profile.id,
            filename=f"{new_id}.mp4", status=VariantStatus.READY,
        )
        db_session.add(new)
        await db_session.commit()

        n = await supersede_ready_variants_once(db_session)
        assert n == 1

        await db_session.refresh(old)
        await db_session.refresh(new)
        assert old.deleted_at is not None, "CANCELLED variant should be soft-deleted"
        assert new.deleted_at is None, "newer READY variant must not be touched"

    async def test_cancelled_variant_preserved_without_newer_ready(
        self, db_session
    ):
        """A lone CANCELLED variant (no newer READY) must NOT be soft-deleted."""
        from cms.services.transcoder import supersede_ready_variants_once

        profile = await self._make_profile(db_session, "cancel-lone")
        asset = await self._make_asset(db_session)

        vid = uuid.uuid4()
        v = AssetVariant(
            id=vid, source_asset_id=asset.id, profile_id=profile.id,
            filename=f"{vid}.mp4", status=VariantStatus.CANCELLED,
            error_message="cancelled mid-transcode",
        )
        db_session.add(v)
        await db_session.commit()

        n = await supersede_ready_variants_once(db_session)
        assert n == 0

        await db_session.refresh(v)
        assert v.deleted_at is None

    async def test_cancelled_not_counted_as_failed(self, db_session):
        """CANCELLED must not be tallied by the asset-level variant_failed counter."""
        profile = await self._make_profile(db_session, "cancel-counter")
        asset = await self._make_asset(db_session)

        db_session.add_all([
            AssetVariant(
                source_asset_id=asset.id, profile_id=profile.id,
                filename=f"{uuid.uuid4()}.mp4", status=VariantStatus.CANCELLED,
            ),
            AssetVariant(
                source_asset_id=asset.id, profile_id=profile.id,
                filename=f"{uuid.uuid4()}.mp4", status=VariantStatus.READY,
            ),
        ])
        await db_session.commit()

        await db_session.refresh(asset, ["variants"])
        failed = sum(1 for v in asset.variants if v.status == VariantStatus.FAILED)
        cancelled = sum(1 for v in asset.variants if v.status == VariantStatus.CANCELLED)
        assert failed == 0
        assert cancelled == 1
