"""Slideshow resolver, manifest version, and capability gate tests.

Covers the new code in:
- ``cms/services/slideshow_resolver.py`` (plan_slideshow,
  build_fetch_for_slideshow, slideshow_readiness, resolved checksum)
- ``cms/routers/assets.py`` (manifest_version baked into Asset.checksum)
- ``cms/routers/schedules.py`` (slideshow_v1 capability gate)
- ``cms/routers/devices.py`` (slideshow_v1 gate on default-asset)
- ``cms/services/scheduler.py`` (per-device resolved manifest checksum
  in ScheduleEntry + default_asset_checksum)

These exercise hand-built ORM objects so we don't depend on the variant
worker pipeline.
"""

from __future__ import annotations

import uuid

import pytest

from cms.models.asset import Asset, AssetType, AssetVariant, VariantStatus
from cms.models.device import Device, DeviceGroup, DeviceStatus
from cms.models.device_profile import DeviceProfile
from cms.models.slideshow_slide import SlideshowSlide
from cms.schemas.protocol import CAPABILITY_SLIDESHOW_V1


# ---------------------------------------------------------------------------
# Test fixtures (module-local helpers)
# ---------------------------------------------------------------------------

async def _seed_image(db, *, filename, is_global=True, checksum="img-sha"):
    a = Asset(
        filename=filename,
        asset_type=AssetType.IMAGE,
        size_bytes=100,
        checksum=checksum,
        is_global=is_global,
    )
    db.add(a)
    await db.commit()
    await db.refresh(a)
    return a


async def _seed_video(db, *, filename, is_global=True, checksum="vid-sha", duration=10.0):
    a = Asset(
        filename=filename,
        asset_type=AssetType.VIDEO,
        size_bytes=200,
        checksum=checksum,
        duration_seconds=duration,
        is_global=is_global,
    )
    db.add(a)
    await db.commit()
    await db.refresh(a)
    return a


async def _seed_profile(db, name="p1"):
    p = DeviceProfile(name=name)
    db.add(p)
    await db.commit()
    await db.refresh(p)
    return p


async def _seed_variant(
    db, *, source, profile, status=VariantStatus.READY, checksum="v-sha", size=50,
    ext="mp4",
):
    v = AssetVariant(
        source_asset_id=source.id,
        profile_id=profile.id,
        filename=f"{uuid.uuid4()}.{ext}",
        status=status,
        checksum=checksum,
        size_bytes=size,
    )
    db.add(v)
    await db.commit()
    await db.refresh(v)
    return v


async def _seed_slideshow(db, *, name, slides_data, checksum=""):
    """Seed a slideshow asset directly (bypasses API ACL flow).

    ``slides_data`` is a list of (source_asset, duration_ms, play_to_end).
    """
    ss = Asset(
        filename=name,
        asset_type=AssetType.SLIDESHOW,
        size_bytes=0,
        checksum=checksum,
        is_global=True,
    )
    db.add(ss)
    await db.commit()
    await db.refresh(ss)
    for idx, (src, dur_ms, pte) in enumerate(slides_data):
        db.add(SlideshowSlide(
            slideshow_asset_id=ss.id,
            source_asset_id=src.id,
            position=idx,
            duration_ms=dur_ms,
            play_to_end=pte,
        ))
    await db.commit()
    await db.refresh(ss)
    return ss


async def _seed_group(db, name="g"):
    g = DeviceGroup(name=name)
    db.add(g)
    await db.commit()
    await db.refresh(g)
    return g


async def _seed_device(
    db, *, did, group=None, profile=None,
    capabilities=None, status=DeviceStatus.ADOPTED,
):
    d = Device(
        id=did,
        name=did,
        status=status,
        capabilities=list(capabilities or []),
        group_id=group.id if group else None,
        profile_id=profile.id if profile else None,
    )
    db.add(d)
    await db.commit()
    await db.refresh(d)
    return d


# ---------------------------------------------------------------------------
# Resolver tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestSlideshowResolver:
    async def test_plan_returns_slides_with_variant_checksum(self, db_session):
        from cms.services.slideshow_resolver import plan_slideshow

        profile = await _seed_profile(db_session, "px")
        img = await _seed_image(db_session, filename="r1.png")
        vid = await _seed_video(db_session, filename="r1.mp4")
        await _seed_variant(
            db_session, source=img, profile=profile,
            checksum="img-variant", ext="jpg",
        )
        await _seed_variant(
            db_session, source=vid, profile=profile,
            checksum="vid-variant", ext="mp4",
        )
        ss = await _seed_slideshow(
            db_session, name="ss-ok",
            slides_data=[(img, 5000, False), (vid, 8000, True)],
            checksum="manifest-base",
        )
        plan = await plan_slideshow(ss, profile.id, db_session)
        assert plan.ready
        assert [s.checksum for s in plan.slides] == ["img-variant", "vid-variant"]
        assert [s.duration_ms for s in plan.slides] == [5000, 8000]
        assert plan.slides[1].play_to_end is True

    async def test_plan_picks_latest_ready_variant(self, db_session):
        """When multiple READY variants exist for the same (asset,profile),
        the resolver picks the latest one (created_at DESC, id DESC tiebreak)."""
        from cms.services.slideshow_resolver import plan_slideshow

        profile = await _seed_profile(db_session, "py")
        img = await _seed_image(db_session, filename="multi.png")
        # First variant
        v_old = await _seed_variant(
            db_session, source=img, profile=profile, checksum="old", ext="jpg",
        )
        # Second variant with explicitly newer created_at
        from datetime import datetime, timezone, timedelta
        v_old.created_at = datetime.now(timezone.utc) - timedelta(seconds=10)
        await db_session.commit()
        v_new = await _seed_variant(
            db_session, source=img, profile=profile, checksum="new", ext="jpg",
        )
        v_new.created_at = datetime.now(timezone.utc)
        await db_session.commit()

        ss = await _seed_slideshow(
            db_session, name="ss-latest",
            slides_data=[(img, 5000, False)], checksum="m",
        )
        plan = await plan_slideshow(ss, profile.id, db_session)
        assert plan.slides[0].checksum == "new"

    async def test_plan_blocker_for_inflight_variant(self, db_session):
        from cms.services.slideshow_resolver import (
            plan_slideshow, BLOCKER_VARIANT_PROCESSING,
        )

        profile = await _seed_profile(db_session, "pinflight")
        img = await _seed_image(db_session, filename="inflight.png")
        await _seed_variant(
            db_session, source=img, profile=profile,
            status=VariantStatus.PROCESSING, ext="jpg",
        )
        ss = await _seed_slideshow(
            db_session, name="ss-inflight",
            slides_data=[(img, 5000, False)], checksum="m",
        )
        plan = await plan_slideshow(ss, profile.id, db_session)
        assert not plan.ready
        assert plan.blockers[0].status == BLOCKER_VARIANT_PROCESSING

    async def test_plan_blocker_for_failed_variant(self, db_session):
        from cms.services.slideshow_resolver import (
            plan_slideshow, BLOCKER_VARIANT_FAILED,
        )

        profile = await _seed_profile(db_session, "pfail")
        img = await _seed_image(db_session, filename="fail.png")
        await _seed_variant(
            db_session, source=img, profile=profile,
            status=VariantStatus.FAILED, ext="jpg",
        )
        ss = await _seed_slideshow(
            db_session, name="ss-fail",
            slides_data=[(img, 5000, False)], checksum="m",
        )
        plan = await plan_slideshow(ss, profile.id, db_session)
        assert not plan.ready
        assert plan.blockers[0].status == BLOCKER_VARIANT_FAILED

    async def test_plan_blocker_for_soft_deleted_source(self, db_session):
        from datetime import datetime, timezone

        from cms.services.slideshow_resolver import (
            plan_slideshow, BLOCKER_SOURCE_DELETED,
        )

        profile = await _seed_profile(db_session, "pdel")
        img = await _seed_image(db_session, filename="will-delete.png")
        ss = await _seed_slideshow(
            db_session, name="ss-deleted",
            slides_data=[(img, 5000, False)], checksum="m",
        )
        # Soft-delete the source after the slideshow was created.
        img.deleted_at = datetime.now(timezone.utc)
        await db_session.commit()

        plan = await plan_slideshow(ss, profile.id, db_session)
        assert not plan.ready
        assert plan.blockers[0].status == BLOCKER_SOURCE_DELETED

    async def test_plan_no_profile_uses_raw_source(self, db_session):
        """Devices without a profile (no transcoding) use raw source URLs."""
        from cms.services.slideshow_resolver import plan_slideshow

        img = await _seed_image(db_session, filename="raw.png", checksum="raw-img")
        ss = await _seed_slideshow(
            db_session, name="ss-raw",
            slides_data=[(img, 5000, False)], checksum="m",
        )
        plan = await plan_slideshow(ss, None, db_session)
        assert plan.ready
        assert plan.slides[0].checksum == "raw-img"

    async def test_plan_no_variant_for_profile_falls_back_to_source(self, db_session):
        from cms.services.slideshow_resolver import plan_slideshow

        profile = await _seed_profile(db_session, "p-empty")
        img = await _seed_image(db_session, filename="novar.png", checksum="raw")
        ss = await _seed_slideshow(
            db_session, name="ss-novar",
            slides_data=[(img, 5000, False)], checksum="m",
        )
        plan = await plan_slideshow(ss, profile.id, db_session)
        assert plan.ready
        assert plan.slides[0].checksum == "raw"

    async def test_resolved_checksum_changes_with_variant_change(self, db_session):
        """Re-transcoding a variant must flip the per-device manifest hash."""
        from cms.services.slideshow_resolver import resolved_slideshow_checksum

        profile = await _seed_profile(db_session, "p-vary")
        img = await _seed_image(db_session, filename="vary.png")
        await _seed_variant(
            db_session, source=img, profile=profile, checksum="v1", ext="jpg",
        )
        ss = await _seed_slideshow(
            db_session, name="ss-vary",
            slides_data=[(img, 5000, False)], checksum="manifest-base",
        )
        c1 = await resolved_slideshow_checksum(ss, profile.id, db_session)

        # Add a newer READY variant — should yield a different checksum.
        from datetime import datetime, timezone
        v2 = await _seed_variant(
            db_session, source=img, profile=profile, checksum="v2", ext="jpg",
        )
        v2.created_at = datetime.now(timezone.utc)
        await db_session.commit()

        c2 = await resolved_slideshow_checksum(ss, profile.id, db_session)
        assert c1 != c2 and c1 is not None and c2 is not None


# ---------------------------------------------------------------------------
# Manifest version stability tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestManifestVersion:
    async def test_create_writes_structural_manifest_hash(self, client, db_session):
        img = await _seed_image(db_session, filename="mv1.png")
        resp = await client.post(
            "/api/assets/slideshow",
            json={
                "name": "mv-show",
                "slides": [
                    {"source_asset_id": str(img.id), "duration_ms": 5000}
                ],
            },
        )
        assert resp.status_code == 201, resp.text
        sid = uuid.UUID(resp.json()["id"])
        ss = await db_session.get(Asset, sid)
        assert ss.checksum and ss.checksum != ""
        assert len(ss.checksum) == 64  # SHA-256 hexdigest

    async def test_replace_changes_manifest_hash(self, client, db_session):
        img1 = await _seed_image(db_session, filename="mv2-a.png")
        img2 = await _seed_image(db_session, filename="mv2-b.png")
        create = await client.post(
            "/api/assets/slideshow",
            json={
                "name": "mv-edit",
                "slides": [
                    {"source_asset_id": str(img1.id), "duration_ms": 5000}
                ],
            },
        )
        sid = uuid.UUID(create.json()["id"])
        ss = await db_session.get(Asset, sid)
        before = ss.checksum

        put = await client.put(
            f"/api/assets/{sid}/slides",
            json={
                "slides": [
                    {"source_asset_id": str(img1.id), "duration_ms": 5000},
                    {"source_asset_id": str(img2.id), "duration_ms": 7000},
                ]
            },
        )
        assert put.status_code == 200, put.text
        await db_session.refresh(ss)
        assert ss.checksum != before
        assert len(ss.checksum) == 64


# ---------------------------------------------------------------------------
# Capability gate tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestCapabilityGate:
    async def test_schedule_create_blocks_incompatible_device(
        self, client, db_session
    ):
        img = await _seed_image(db_session, filename="cap1.png")
        ss = await _seed_slideshow(
            db_session, name="cap-ss-1",
            slides_data=[(img, 5000, False)], checksum="m",
        )
        g = await _seed_group(db_session, "cap-g-1")
        await _seed_device(
            db_session, did="cap-d-1", group=g, capabilities=[],
        )
        resp = await client.post(
            "/api/schedules",
            json={
                "name": "cap-sched-1",
                "asset_id": str(ss.id),
                "group_id": str(g.id),
                "start_time": "08:00:00",
                "end_time": "09:00:00",
            },
        )
        assert resp.status_code == 422
        assert "slideshow_v1" in resp.text

    async def test_schedule_create_allows_compatible_device(
        self, client, db_session
    ):
        img = await _seed_image(db_session, filename="cap2.png")
        ss = await _seed_slideshow(
            db_session, name="cap-ss-2",
            slides_data=[(img, 5000, False)], checksum="m",
        )
        g = await _seed_group(db_session, "cap-g-2")
        await _seed_device(
            db_session, did="cap-d-2", group=g,
            capabilities=[CAPABILITY_SLIDESHOW_V1],
        )
        resp = await client.post(
            "/api/schedules",
            json={
                "name": "cap-sched-2",
                "asset_id": str(ss.id),
                "group_id": str(g.id),
                "start_time": "08:00:00",
                "end_time": "09:00:00",
            },
        )
        assert resp.status_code == 201, resp.text

    async def test_device_default_asset_blocks_incompatible(
        self, client, db_session
    ):
        img = await _seed_image(db_session, filename="cap3.png")
        ss = await _seed_slideshow(
            db_session, name="cap-ss-3",
            slides_data=[(img, 5000, False)], checksum="m",
        )
        await _seed_device(db_session, did="cap-d-3", capabilities=[])
        resp = await client.patch(
            "/api/devices/cap-d-3",
            json={"default_asset_id": str(ss.id)},
        )
        assert resp.status_code == 422
        assert "slideshow_v1" in resp.text

    async def test_group_default_asset_blocks_incompatible(
        self, client, db_session
    ):
        img = await _seed_image(db_session, filename="cap4.png")
        ss = await _seed_slideshow(
            db_session, name="cap-ss-4",
            slides_data=[(img, 5000, False)], checksum="m",
        )
        g = await _seed_group(db_session, "cap-g-4")
        await _seed_device(
            db_session, did="cap-d-4", group=g, capabilities=[],
        )
        resp = await client.patch(
            f"/api/devices/groups/{g.id}",
            json={"default_asset_id": str(ss.id)},
        )
        assert resp.status_code == 422
        assert "slideshow_v1" in resp.text


# ---------------------------------------------------------------------------
# Readiness API surface
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestReadinessApi:
    async def test_get_slides_with_profile_includes_readiness(
        self, client, db_session
    ):
        profile = await _seed_profile(db_session, "p-rd")
        img = await _seed_image(db_session, filename="rd.png")
        ss = await _seed_slideshow(
            db_session, name="rd-ss",
            slides_data=[(img, 5000, False)], checksum="m",
        )
        resp = await client.get(
            f"/api/assets/{ss.id}/slides?profile_id={profile.id}"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "readiness" in body
        # No variant — falls back to source — ready.
        assert body["readiness"]["ready"] is True

    async def test_get_slides_readiness_reports_inflight(
        self, client, db_session
    ):
        profile = await _seed_profile(db_session, "p-rd2")
        img = await _seed_image(db_session, filename="rd2.png")
        await _seed_variant(
            db_session, source=img, profile=profile,
            status=VariantStatus.PROCESSING, ext="jpg",
        )
        ss = await _seed_slideshow(
            db_session, name="rd-ss-2",
            slides_data=[(img, 5000, False)], checksum="m",
        )
        resp = await client.get(
            f"/api/assets/{ss.id}/slides?profile_id={profile.id}"
        )
        body = resp.json()
        assert body["readiness"]["ready"] is False
        assert body["readiness"]["blockers"][0]["status"] == "variant_processing"
