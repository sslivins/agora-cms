"""Tests for reconcile_transcode_failure_notifications_once.

The reconciler (run in the CMS reaper loop) pushes permanent transcode
failures into the notification bell: one ``scope="system"``, ``level="error"``
notification per currently-FAILED, non-soft-deleted variant, deduped on
``details['variant_id']``.  It self-heals — notifications whose variant is no
longer FAILED are pruned.
"""

import uuid

import pytest
from sqlalchemy import select


async def _mk_failed_variant(db, *, deleted=False, error="kaboom"):
    from cms.models.asset import Asset, AssetType, AssetVariant, VariantStatus
    from cms.models.device_profile import DeviceProfile
    from datetime import datetime, timezone

    asset = Asset(
        filename=f"{uuid.uuid4().hex}.mp4", asset_type=AssetType.VIDEO,
        size_bytes=1000, checksum=uuid.uuid4().hex[:8],
    )
    profile = DeviceProfile(name=f"rec-{uuid.uuid4().hex[:8]}", video_codec="h264")
    db.add_all([asset, profile])
    await db.flush()
    variant = AssetVariant(
        source_asset_id=asset.id, profile_id=profile.id,
        filename=f"{uuid.uuid4()}.mp4", status=VariantStatus.FAILED,
        error_message=error,
        deleted_at=datetime.now(timezone.utc) if deleted else None,
    )
    db.add(variant)
    await db.commit()
    return asset, variant


@pytest.mark.asyncio
class TestReconcileTranscodeFailureNotifications:
    async def test_creates_one_notification_per_failed(self, db_session):
        from cms.models.notification import Notification
        from cms.services.transcoder import (
            TRANSCODE_FAIL_NOTIFICATION_TITLE,
            reconcile_transcode_failure_notifications_once,
        )

        _, variant = await _mk_failed_variant(db_session)

        created = await reconcile_transcode_failure_notifications_once(db_session)
        assert created == 1

        db_session.expunge_all()
        notifs = (await db_session.execute(
            select(Notification).where(
                Notification.title == TRANSCODE_FAIL_NOTIFICATION_TITLE
            )
        )).scalars().all()
        assert len(notifs) == 1
        n = notifs[0]
        assert n.scope == "system"
        assert n.level == "error"
        assert n.details["variant_id"] == str(variant.id)

    async def test_idempotent_no_duplicate(self, db_session):
        from cms.models.notification import Notification
        from cms.services.transcoder import (
            TRANSCODE_FAIL_NOTIFICATION_TITLE,
            reconcile_transcode_failure_notifications_once,
        )

        await _mk_failed_variant(db_session)

        first = await reconcile_transcode_failure_notifications_once(db_session)
        second = await reconcile_transcode_failure_notifications_once(db_session)
        assert first == 1
        assert second == 0

        db_session.expunge_all()
        count = len((await db_session.execute(
            select(Notification).where(
                Notification.title == TRANSCODE_FAIL_NOTIFICATION_TITLE
            )
        )).scalars().all())
        assert count == 1

    async def test_ignores_soft_deleted_variants(self, db_session):
        from cms.services.transcoder import (
            reconcile_transcode_failure_notifications_once,
        )

        await _mk_failed_variant(db_session, deleted=True)

        created = await reconcile_transcode_failure_notifications_once(db_session)
        assert created == 0

    async def test_prunes_stale_notifications(self, db_session):
        """A notification whose variant is no longer FAILED is pruned."""
        from cms.models.asset import AssetVariant, VariantStatus
        from cms.models.notification import Notification
        from cms.services.transcoder import (
            TRANSCODE_FAIL_NOTIFICATION_TITLE,
            reconcile_transcode_failure_notifications_once,
        )

        _, variant = await _mk_failed_variant(db_session)
        created = await reconcile_transcode_failure_notifications_once(db_session)
        assert created == 1

        # Variant recovers to READY → its notification should be pruned.
        variant.status = VariantStatus.READY
        db_session.add(variant)
        await db_session.commit()

        created2 = await reconcile_transcode_failure_notifications_once(db_session)
        assert created2 == 0

        db_session.expunge_all()
        notifs = (await db_session.execute(
            select(Notification).where(
                Notification.title == TRANSCODE_FAIL_NOTIFICATION_TITLE
            )
        )).scalars().all()
        assert notifs == []
