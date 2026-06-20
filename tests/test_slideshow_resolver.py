"""Slideshow resolver, manifest version, and capability gate tests.

Covers the new code in:
- ``cms/services/slideshow_resolver.py`` (plan_slideshow,
  build_fetch_for_slideshow, slideshow_readiness, resolved checksum)
- ``cms/routers/assets.py`` (manifest_content_hash baked into Asset.checksum)
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
from cms.schemas.protocol import (
    CAPABILITY_SLIDESHOW_COMPOSED_V1,
    CAPABILITY_SLIDESHOW_V1,
)


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


async def _seed_composed(
    db, *, filename, checksum="cmp-sha", size=4096,
    is_global=True, source_asset_ids=None,
):
    """Seed a COMPOSED asset (+ its ComposedSlide row).

    ``checksum`` set => published (a bundle exists).  Pass ``checksum=None``
    to model an unpublished composed slide.  ``source_asset_ids`` populates
    the bundle's referenced media (siblings) — stored stringified, matching
    the publish layer.
    """
    from cms.models.composed_slide import ComposedSlide

    a = Asset(
        filename=filename,
        asset_type=AssetType.COMPOSED,
        size_bytes=size,
        checksum=checksum,
        is_global=is_global,
    )
    db.add(a)
    await db.commit()
    await db.refresh(a)
    cs = ComposedSlide(
        asset_id=a.id,
        layout_json={"widgets": []},
        is_draft=False,
        bundle_source_asset_ids=(
            None if source_asset_ids is None
            else [str(aid) for aid in source_asset_ids]
        ),
    )
    db.add(cs)
    await db.commit()
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

    ``slides_data`` is a list of ``(source_asset, duration_ms, play_to_end)``
    or ``(source_asset, duration_ms, play_to_end, transition, transition_ms)``
    for tests that need to assert on transition propagation.
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
    for idx, row in enumerate(slides_data):
        if len(row) == 3:
            src, dur_ms, pte = row
            trans, trans_ms = "cut", 600
        else:
            src, dur_ms, pte, trans, trans_ms = row
        db.add(SlideshowSlide(
            slideshow_asset_id=ss.id,
            source_asset_id=src.id,
            position=idx,
            duration_ms=dur_ms,
            play_to_end=pte,
            transition=trans,
            transition_ms=trans_ms,
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

    async def test_empty_slideshow_is_not_ready(self, db_session):
        """A 0-slide slideshow (e.g. a fresh AI-assistant draft) must never
        be ready, so it can't be pushed to a device as an empty manifest."""
        from cms.services.slideshow_resolver import (
            plan_slideshow,
            resolved_slideshow_checksum,
        )

        profile = await _seed_profile(db_session, "pempty")
        ss = await _seed_slideshow(
            db_session, name="ss-empty", slides_data=[], checksum="empty-base",
        )
        plan = await plan_slideshow(ss, profile.id, db_session)
        assert plan.slides == []
        assert plan.ready is False
        # The device-facing checksum is None for a not-ready plan, so the
        # empty draft never lands in a ScheduleEntry / default-asset.
        assert await resolved_slideshow_checksum(ss, profile.id, db_session) is None


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

    async def test_plan_propagates_transition_fields(self, db_session):
        """Phase 1a: per-slide transition + transition_ms must reach the
        wire ``SlideDescriptor`` via the plan."""
        from cms.services.slideshow_resolver import plan_slideshow

        profile = await _seed_profile(db_session, "p-trans")
        img = await _seed_image(db_session, filename="tr1.png", checksum="raw")
        ss = await _seed_slideshow(
            db_session, name="ss-trans",
            slides_data=[
                (img, 3000, False, "fade", 800),
                (img, 4000, False, "cut", 600),
            ],
            checksum="m",
        )
        plan = await plan_slideshow(ss, profile.id, db_session)
        assert plan.ready
        assert [s.transition for s in plan.slides] == ["fade", "cut"]
        assert [s.transition_ms for s in plan.slides] == [800, 600]

    async def test_build_fetch_emits_wall_clock_fields(self, db_session, app):
        """Phase 1b: ``build_fetch_for_slideshow`` must populate
        ``manifest_schema_version``, ``cycle_duration_ms``, and
        ``started_at`` on the outbound ``FetchAssetMessage``.

        ``cycle_duration_ms`` = sum(per-slide duration_ms).
        ``started_at`` = floor(now_utc, cycle_duration_ms) as
        ISO-8601 UTC ("Z" suffix).
        """
        from cms.services.slideshow_resolver import build_fetch_for_slideshow

        profile = await _seed_profile(db_session, "p-wc")
        img = await _seed_image(db_session, filename="wc1.png", checksum="rwc")
        ss = await _seed_slideshow(
            db_session, name="ss-wc",
            slides_data=[(img, 3000, False), (img, 4500, False)],
            checksum="mwc",
        )
        device = await _seed_device(
            db_session, did="dev-wc", profile=profile,
            capabilities=[CAPABILITY_SLIDESHOW_V1],
        )

        fetch = await build_fetch_for_slideshow(
            ss, device, "https://cms.example", db_session,
        )
        assert fetch is not None
        assert fetch.manifest_schema_version == "1.5"
        assert fetch.cycle_duration_ms == 7500
        assert fetch.started_at is not None
        assert fetch.started_at.endswith("Z")
        # Cycle-floor invariant: parsing the anchor must yield an integer
        # millisecond count that's an exact multiple of cycle_duration_ms.
        from datetime import datetime
        anchor = datetime.fromisoformat(fetch.started_at.replace("Z", "+00:00"))
        anchor_ms = int(anchor.timestamp() * 1000)
        assert anchor_ms % fetch.cycle_duration_ms == 0


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


# ---------------------------------------------------------------------------
# Composed-in-slideshow tests (Phase 5 / PR A)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestComposedSlideshowMember:
    async def test_published_composed_member_emits_descriptor_with_siblings(
        self, db_session, app
    ):
        """A published composed member resolves to a ``composed``
        SlideDescriptor whose ``siblings`` carry the referenced video
        (resolved to its READY variant for the device profile)."""
        from cms.services.slideshow_resolver import build_fetch_for_slideshow

        profile = await _seed_profile(db_session, "p-cmp")
        vid = await _seed_video(
            db_session, filename="cmp-sib.mp4", checksum="sibsrc",
        )
        await _seed_variant(
            db_session, source=vid, profile=profile,
            status=VariantStatus.READY, checksum="sibvar",
        )
        cmp_asset = await _seed_composed(
            db_session, filename="cmp-mem.html", checksum="cmpsha",
            source_asset_ids=[vid.id],
        )
        ss = await _seed_slideshow(
            db_session, name="ss-cmp",
            slides_data=[(cmp_asset, 8000, False)], checksum="mcmp",
        )
        device = await _seed_device(
            db_session, did="dev-cmp", profile=profile,
            capabilities=[
                CAPABILITY_SLIDESHOW_V1, CAPABILITY_SLIDESHOW_COMPOSED_V1,
            ],
        )

        fetch = await build_fetch_for_slideshow(
            ss, device, "https://cms.example", db_session,
        )
        assert fetch is not None
        assert len(fetch.slides) == 1
        slide = fetch.slides[0]
        assert slide.asset_type == "composed"
        assert slide.download_url
        assert slide.checksum == "cmpsha"
        assert slide.siblings is not None and len(slide.siblings) == 1
        sib = slide.siblings[0]
        assert sib.name == "cmp-sib.mp4"
        assert sib.asset_type == "video"
        assert sib.checksum == "sibvar"
        assert sib.download_url

    async def test_published_composed_member_no_siblings(
        self, db_session, app
    ):
        """An all-text composed member (no referenced media) ships a
        composed descriptor with empty/None siblings."""
        from cms.services.slideshow_resolver import build_fetch_for_slideshow

        profile = await _seed_profile(db_session, "p-cmp2")
        cmp_asset = await _seed_composed(
            db_session, filename="cmp-text.html", checksum="cmptext",
            source_asset_ids=None,
        )
        ss = await _seed_slideshow(
            db_session, name="ss-cmp2",
            slides_data=[(cmp_asset, 6000, False)], checksum="mcmp2",
        )
        device = await _seed_device(
            db_session, did="dev-cmp2", profile=profile,
            capabilities=[
                CAPABILITY_SLIDESHOW_V1, CAPABILITY_SLIDESHOW_COMPOSED_V1,
            ],
        )

        fetch = await build_fetch_for_slideshow(
            ss, device, "https://cms.example", db_session,
        )
        assert fetch is not None
        slide = fetch.slides[0]
        assert slide.asset_type == "composed"
        assert not slide.siblings

    async def test_unpublished_composed_member_blocks(
        self, db_session, app
    ):
        """A composed member with no bundle (checksum=None) makes the
        slideshow not-ready with a ``source_unpublished`` blocker."""
        from cms.services.slideshow_resolver import (
            BLOCKER_SOURCE_UNPUBLISHED,
            plan_slideshow,
        )

        profile = await _seed_profile(db_session, "p-cmp3")
        cmp_asset = await _seed_composed(
            db_session, filename="cmp-draft.html", checksum=None,
        )
        ss = await _seed_slideshow(
            db_session, name="ss-cmp3",
            slides_data=[(cmp_asset, 5000, False)], checksum="mcmp3",
        )

        plan = await plan_slideshow(ss, profile.id, db_session)
        assert plan.ready is False
        assert any(
            b.status == BLOCKER_SOURCE_UNPUBLISHED for b in plan.blockers
        )

    async def test_inflight_sibling_blocks(self, db_session, app):
        """A composed member whose referenced video has a non-READY
        variant for the device profile blocks with ``variant_processing``."""
        from cms.services.slideshow_resolver import (
            BLOCKER_VARIANT_PROCESSING,
            plan_slideshow,
        )

        profile = await _seed_profile(db_session, "p-cmp4")
        vid = await _seed_video(
            db_session, filename="cmp-inflight.mp4", checksum="srcq",
        )
        await _seed_variant(
            db_session, source=vid, profile=profile,
            status=VariantStatus.PROCESSING, checksum="",
        )
        cmp_asset = await _seed_composed(
            db_session, filename="cmp-mem4.html", checksum="cmp4",
            source_asset_ids=[vid.id],
        )
        ss = await _seed_slideshow(
            db_session, name="ss-cmp4",
            slides_data=[(cmp_asset, 5000, False)], checksum="mcmp4",
        )

        plan = await plan_slideshow(ss, profile.id, db_session)
        assert plan.ready is False
        assert any(
            b.status == BLOCKER_VARIANT_PROCESSING for b in plan.blockers
        )

    async def test_sibling_checksum_change_flips_manifest_hash(
        self, db_session, app
    ):
        """Re-transcoding a composed member's sibling (new variant
        checksum) flips the resolved manifest hash so the device refetches."""
        from cms.services.slideshow_resolver import build_fetch_for_slideshow

        profile = await _seed_profile(db_session, "p-cmp5")
        vid = await _seed_video(
            db_session, filename="cmp-fold.mp4", checksum="srcf",
        )
        var = await _seed_variant(
            db_session, source=vid, profile=profile,
            status=VariantStatus.READY, checksum="foldA",
        )
        cmp_asset = await _seed_composed(
            db_session, filename="cmp-mem5.html", checksum="cmp5",
            source_asset_ids=[vid.id],
        )
        ss = await _seed_slideshow(
            db_session, name="ss-cmp5",
            slides_data=[(cmp_asset, 5000, False)], checksum="mcmp5",
        )
        device = await _seed_device(
            db_session, did="dev-cmp5", profile=profile,
            capabilities=[
                CAPABILITY_SLIDESHOW_V1, CAPABILITY_SLIDESHOW_COMPOSED_V1,
            ],
        )

        fetch1 = await build_fetch_for_slideshow(
            ss, device, "https://cms.example", db_session,
        )
        var.checksum = "foldB"
        db_session.add(var)
        await db_session.commit()
        fetch2 = await build_fetch_for_slideshow(
            ss, device, "https://cms.example", db_session,
        )
        assert fetch1.checksum and fetch2.checksum
        assert fetch1.checksum != fetch2.checksum


# ---------------------------------------------------------------------------
# Composed capability gate (slideshow_composed_v1)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestComposedCapabilityGate:
    async def test_schedule_create_blocks_device_without_composed_cap(
        self, client, db_session
    ):
        """A slideshow containing a composed member must be rejected for a
        device that has ``slideshow_v1`` but not ``slideshow_composed_v1``."""
        cmp_asset = await _seed_composed(
            db_session, filename="capc-mem.html", checksum="capc",
        )
        ss = await _seed_slideshow(
            db_session, name="capc-ss",
            slides_data=[(cmp_asset, 5000, False)], checksum="m",
        )
        g = await _seed_group(db_session, "capc-g")
        await _seed_device(
            db_session, did="capc-d", group=g,
            capabilities=[CAPABILITY_SLIDESHOW_V1],
        )
        resp = await client.post(
            "/api/schedules",
            json={
                "name": "capc-sched",
                "asset_id": str(ss.id),
                "group_id": str(g.id),
                "start_time": "08:00:00",
                "end_time": "09:00:00",
            },
        )
        assert resp.status_code == 422
        assert "slideshow_composed_v1" in resp.text

    async def test_schedule_create_allows_device_with_composed_cap(
        self, client, db_session
    ):
        """Same slideshow is allowed when the device advertises both
        ``slideshow_v1`` and ``slideshow_composed_v1``."""
        cmp_asset = await _seed_composed(
            db_session, filename="capc2-mem.html", checksum="capc2",
        )
        ss = await _seed_slideshow(
            db_session, name="capc2-ss",
            slides_data=[(cmp_asset, 5000, False)], checksum="m",
        )
        g = await _seed_group(db_session, "capc2-g")
        await _seed_device(
            db_session, did="capc2-d", group=g,
            capabilities=[
                CAPABILITY_SLIDESHOW_V1, CAPABILITY_SLIDESHOW_COMPOSED_V1,
            ],
        )
        resp = await client.post(
            "/api/schedules",
            json={
                "name": "capc2-sched",
                "asset_id": str(ss.id),
                "group_id": str(g.id),
                "start_time": "08:00:00",
                "end_time": "09:00:00",
            },
        )
        assert resp.status_code == 201, resp.text


# ---------------------------------------------------------------------------
# Pure-function tests: deck shuffle seed + resolved-checksum fold (agora#261)
# ---------------------------------------------------------------------------

class TestShuffleSeedAndChecksumFold:
    """Unit coverage for the manifest 1.4 deck-shuffle + per-slide
    effect_direction folding, exercised through the resolver's pure
    helpers so we don't need the DB / variant pipeline.
    """

    def test_shuffle_seed_is_stable_and_js_safe(self) -> None:
        from cms.services.slideshow_resolver import _shuffle_seed_for_asset

        aid = uuid.UUID("12345678-1234-5678-1234-567812345678")
        s1 = _shuffle_seed_for_asset(aid)
        s2 = _shuffle_seed_for_asset(aid)
        assert s1 == s2  # deterministic across calls / processes
        assert 0 <= s1 <= 0x7FFFFFFF  # 31-bit, JS-safe non-negative int
        # A different asset id yields a different seed (overwhelmingly likely).
        other = _shuffle_seed_for_asset(uuid.uuid4())
        assert isinstance(other, int)

    def _plan(self, effect_direction: str = "in"):
        from cms.services.slideshow_resolver import _SlidePlan

        return _SlidePlan(
            position=0,
            source_asset_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
            source_filename="a.png",
            source_asset_type=AssetType.IMAGE,
            duration_ms=2000,
            play_to_end=False,
            fit="cover",
            effect="ken_burns",
            effect_direction=effect_direction,
            checksum="sha-a",
        )

    def test_shuffle_bool_folds_into_checksum(self) -> None:
        from cms.services.slideshow_resolver import (
            _compute_resolved_manifest_checksum,
        )

        slides = [self._plan()]
        off = _compute_resolved_manifest_checksum("asset-sha", slides, shuffle=False)
        on = _compute_resolved_manifest_checksum("asset-sha", slides, shuffle=True)
        assert off != on  # toggling shuffle re-pushes the manifest

    def test_effect_direction_folds_into_checksum(self) -> None:
        from cms.services.slideshow_resolver import (
            _compute_resolved_manifest_checksum,
        )

        in_ck = _compute_resolved_manifest_checksum(
            "asset-sha", [self._plan("in")], shuffle=False
        )
        left_ck = _compute_resolved_manifest_checksum(
            "asset-sha", [self._plan("left")], shuffle=False
        )
        assert in_ck != left_ck  # changing KB direction invalidates the cache


# ---------------------------------------------------------------------------
# Tag-mode dynamic playlists (agora#806)
# ---------------------------------------------------------------------------

async def _seed_tag(db, name="promo"):
    from cms.models.tag import Tag

    t = Tag(name=name)
    db.add(t)
    await db.commit()
    await db.refresh(t)
    return t


async def _tag_asset(db, *, asset, tag, when):
    """Create an ``AssetTag`` with an explicit ``created_at`` so tests can
    control the ``tagged_at`` ordering that tag-mode decks rely on."""
    from cms.models.tag import AssetTag

    at = AssetTag(asset_id=asset.id, tag_id=tag.id, created_at=when)
    db.add(at)
    await db.commit()
    await db.refresh(at)
    return at


async def _seed_tag_rule(
    db, *, slideshow, tag, anchor_at=None, default_duration_ms=8000
):
    """Seed a tag-mode deck the hybrid way: append a single ``kind='tag'``
    block to ``slideshow`` and persist the cycle anchor on the asset.

    The legacy 1:1 ``SlideshowTagRule`` model was retired in Phase 1; a
    tag-mode slideshow is now expressed as a slideshow whose deck is one
    tag block, so this helper inserts that block at the next free position.
    """
    from sqlalchemy import func, select

    next_pos = (
        await db.execute(
            select(func.coalesce(func.max(SlideshowSlide.position) + 1, 0)).where(
                SlideshowSlide.slideshow_asset_id == slideshow.id
            )
        )
    ).scalar_one()
    s = SlideshowSlide(
        slideshow_asset_id=slideshow.id,
        kind="tag",
        source_asset_id=None,
        tag_id=tag.id,
        tag_order_by="tagged_at",
        position=next_pos,
        duration_ms=default_duration_ms,
    )
    db.add(s)
    slideshow.slideshow_anchor_at = anchor_at
    await db.commit()
    await db.refresh(s)
    return s


@pytest.mark.asyncio
class TestTagModeSlideshow:
    async def test_resolves_members_in_tagged_at_order(self, db_session):
        """A tag-mode deck builds its slide list from current tag
        membership, ordered by ``asset_tags.created_at`` (tagged_at)."""
        from datetime import datetime, timedelta, timezone

        from cms.services.slideshow_resolver import plan_slideshow

        profile = await _seed_profile(db_session, "ptag1")
        tag = await _seed_tag(db_session, "promo1")
        a = await _seed_image(db_session, filename="a.png")
        b = await _seed_image(db_session, filename="b.png")
        c = await _seed_image(db_session, filename="c.png")
        for img, ck in ((a, "va"), (b, "vb"), (c, "vc")):
            await _seed_variant(
                db_session, source=img, profile=profile, checksum=ck, ext="jpg",
            )
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        # Tag order (non-alphabetical): c (oldest) -> a -> b (newest).
        await _tag_asset(db_session, asset=c, tag=tag, when=base)
        await _tag_asset(db_session, asset=a, tag=tag, when=base + timedelta(seconds=10))
        await _tag_asset(db_session, asset=b, tag=tag, when=base + timedelta(seconds=20))

        ss = await _seed_slideshow(
            db_session, name="tagdeck", slides_data=[], checksum="tag-base",
        )
        await _seed_tag_rule(db_session, slideshow=ss, tag=tag)

        plan = await plan_slideshow(ss, profile.id, db_session)
        assert plan.ready
        assert [s.source_filename for s in plan.slides] == ["c.png", "a.png", "b.png"]
        assert [s.position for s in plan.slides] == [0, 1, 2]
        # Every member inherits the rule's deck-level duration default.
        assert all(s.duration_ms == 8000 for s in plan.slides)

    async def test_newly_tagged_asset_appends_at_tail(self, db_session):
        """Tagging a new asset appends it at the tail, leaving existing
        members in place (Option B no-restart guarantee)."""
        from datetime import datetime, timedelta, timezone

        from cms.services.slideshow_resolver import plan_slideshow

        profile = await _seed_profile(db_session, "ptag2")
        tag = await _seed_tag(db_session, "promo2")
        a = await _seed_image(db_session, filename="first.png")
        b = await _seed_image(db_session, filename="second.png")
        for img in (a, b):
            await _seed_variant(db_session, source=img, profile=profile, ext="jpg")
        base = datetime(2026, 2, 1, tzinfo=timezone.utc)
        await _tag_asset(db_session, asset=a, tag=tag, when=base)
        await _tag_asset(db_session, asset=b, tag=tag, when=base + timedelta(seconds=10))
        ss = await _seed_slideshow(
            db_session, name="tagdeck2", slides_data=[], checksum="tb",
        )
        await _seed_tag_rule(db_session, slideshow=ss, tag=tag)

        plan1 = await plan_slideshow(ss, profile.id, db_session)
        assert [s.source_filename for s in plan1.slides] == ["first.png", "second.png"]

        c = await _seed_image(db_session, filename="third.png")
        await _seed_variant(db_session, source=c, profile=profile, ext="jpg")
        await _tag_asset(db_session, asset=c, tag=tag, when=base + timedelta(seconds=20))

        plan2 = await plan_slideshow(ss, profile.id, db_session)
        assert [s.source_filename for s in plan2.slides] == [
            "first.png", "second.png", "third.png",
        ]
        assert [s.position for s in plan2.slides] == [0, 1, 2]

    async def test_persisted_anchor_emitted_verbatim(self, db_session, app):
        """A tag rule with a persisted ``anchor_at`` emits it verbatim as
        ``started_at`` — NOT floored to a cycle boundary — so tail appends
        never shift the on-screen slide."""
        from datetime import datetime, timezone

        from cms.services.slideshow_resolver import (
            _format_anchor,
            build_fetch_for_slideshow,
        )

        profile = await _seed_profile(db_session, "ptag4")
        group = await _seed_group(db_session, "gtag4")
        device = await _seed_device(
            db_session, did="dtag4", group=group, profile=profile,
        )
        tag = await _seed_tag(db_session, "promo4")
        img = await _seed_image(db_session, filename="anchor.png")
        await _seed_variant(
            db_session, source=img, profile=profile, checksum="av", ext="jpg",
        )
        await _tag_asset(
            db_session, asset=img, tag=tag,
            when=datetime(2026, 4, 1, tzinfo=timezone.utc),
        )
        ss = await _seed_slideshow(
            db_session, name="tagdeck4", slides_data=[], checksum="tb4",
        )
        anchor = datetime(2026, 4, 1, 12, 0, 0, tzinfo=timezone.utc)
        await _seed_tag_rule(db_session, slideshow=ss, tag=tag, anchor_at=anchor)

        fetch = await build_fetch_for_slideshow(
            ss, device, "https://cms.example", db_session,
        )
        assert fetch is not None
        assert fetch.started_at == _format_anchor(anchor)

    async def test_null_anchor_falls_back_to_floored(self, db_session, app):
        """A tag rule with NULL ``anchor_at`` (created before anchor
        support) falls back to the floored-to-cycle-boundary anchor."""
        from datetime import datetime, timezone

        from cms.services.slideshow_resolver import build_fetch_for_slideshow

        profile = await _seed_profile(db_session, "ptag5")
        group = await _seed_group(db_session, "gtag5")
        device = await _seed_device(
            db_session, did="dtag5", group=group, profile=profile,
        )
        tag = await _seed_tag(db_session, "promo5")
        img = await _seed_image(db_session, filename="floor.png")
        await _seed_variant(
            db_session, source=img, profile=profile, checksum="fv", ext="jpg",
        )
        await _tag_asset(
            db_session, asset=img, tag=tag,
            when=datetime(2026, 5, 1, tzinfo=timezone.utc),
        )
        ss = await _seed_slideshow(
            db_session, name="tagdeck5", slides_data=[], checksum="tb5",
        )
        await _seed_tag_rule(db_session, slideshow=ss, tag=tag, anchor_at=None)

        fetch = await build_fetch_for_slideshow(
            ss, device, "https://cms.example", db_session,
        )
        assert fetch is not None
        anchor = datetime.fromisoformat(fetch.started_at.replace("Z", "+00:00"))
        assert int(anchor.timestamp() * 1000) % fetch.cycle_duration_ms == 0



async def _seed_hybrid_slideshow(db, *, name, entries, checksum="", anchor_at=None):
    """Seed a hybrid-timeline slideshow directly.

    ``entries`` is an ordered list of dicts, each either:
      * ``{"asset": <Asset>, "duration_ms": int, ...}`` — a static slide
      * ``{"tag": <Tag>, "duration_ms": int, ...}`` — a dynamic tag block

    Optional per-entry overrides: ``play_to_end``, ``transition``,
    ``transition_ms``, ``fit``, ``effect``, ``effect_direction``,
    ``member_transition``, ``member_transition_ms`` (tag blocks only).
    ``anchor_at`` sets the persisted per-slideshow cycle anchor.
    """
    ss = Asset(
        filename=name,
        asset_type=AssetType.SLIDESHOW,
        size_bytes=0,
        checksum=checksum,
        is_global=True,
        slideshow_anchor_at=anchor_at,
    )
    db.add(ss)
    await db.commit()
    await db.refresh(ss)
    for idx, e in enumerate(entries):
        is_tag = "tag" in e
        db.add(SlideshowSlide(
            slideshow_asset_id=ss.id,
            kind="tag" if is_tag else "asset",
            source_asset_id=None if is_tag else e["asset"].id,
            tag_id=e["tag"].id if is_tag else None,
            tag_order_by="tagged_at" if is_tag else None,
            position=idx,
            duration_ms=e.get("duration_ms", 5000),
            play_to_end=e.get("play_to_end", False),
            transition=e.get("transition", "cut"),
            transition_ms=e.get("transition_ms", 600),
            fit=e.get("fit", "cover"),
            effect=e.get("effect", "none"),
            effect_direction=e.get("effect_direction", "in"),
            member_transition=e.get("member_transition") if is_tag else None,
            member_transition_ms=e.get("member_transition_ms") if is_tag else None,
        ))
    await db.commit()
    await db.refresh(ss)
    return ss


@pytest.mark.asyncio
class TestHybridTagTimeline:
    """Phase 0 of the hybrid tag-timeline redesign (agora#806 successor):
    a deck is an ordered list of static ``asset`` slides and dynamic
    ``tag`` blocks that expand in-place at resolve time."""

    async def test_asset_only_timeline_unchanged(self, db_session):
        """A timeline of only ``asset``-kind slides resolves exactly like a
        classic manual slideshow (backward-compatibility regression)."""
        from cms.services.slideshow_resolver import plan_slideshow

        profile = await _seed_profile(db_session, "phyb1")
        a = await _seed_image(db_session, filename="one.png")
        b = await _seed_image(db_session, filename="two.png")
        for img in (a, b):
            await _seed_variant(db_session, source=img, profile=profile, ext="jpg")
        ss = await _seed_hybrid_slideshow(
            db_session, name="hyb1",
            entries=[
                {"asset": a, "duration_ms": 4000},
                {"asset": b, "duration_ms": 6000},
            ],
            checksum="hb1",
        )
        plan = await plan_slideshow(ss, profile.id, db_session)
        assert plan.ready
        assert [s.source_filename for s in plan.slides] == ["one.png", "two.png"]
        assert [s.position for s in plan.slides] == [0, 1]
        assert [s.duration_ms for s in plan.slides] == [4000, 6000]

    async def test_tag_block_expands_in_place(self, db_session):
        """A ``tag``-kind slide expands to its members in tagged_at order,
        each inheriting the row's playback columns."""
        from datetime import datetime, timedelta, timezone

        from cms.services.slideshow_resolver import plan_slideshow

        profile = await _seed_profile(db_session, "phyb2")
        tag = await _seed_tag(db_session, "promoH2")
        x = await _seed_image(db_session, filename="x.png")
        y = await _seed_image(db_session, filename="y.png")
        for img in (x, y):
            await _seed_variant(db_session, source=img, profile=profile, ext="jpg")
        base = datetime(2026, 6, 1, tzinfo=timezone.utc)
        await _tag_asset(db_session, asset=y, tag=tag, when=base)
        await _tag_asset(db_session, asset=x, tag=tag, when=base + timedelta(seconds=5))
        ss = await _seed_hybrid_slideshow(
            db_session, name="hyb2",
            entries=[{"tag": tag, "duration_ms": 7000, "transition": "fade"}],
            checksum="hb2",
        )
        plan = await plan_slideshow(ss, profile.id, db_session)
        assert plan.ready
        # tagged_at order: y (older) then x.
        assert [s.source_filename for s in plan.slides] == ["y.png", "x.png"]
        assert [s.position for s in plan.slides] == [0, 1]
        # Members inherit the tag row's playback columns.
        assert all(s.duration_ms == 7000 for s in plan.slides)
        assert all(s.transition == "fade" for s in plan.slides)
        # Image tag members never play-to-end (they use the block dwell).
        assert all(s.play_to_end is False for s in plan.slides)

    async def test_tag_block_video_member_plays_to_end(self, db_session):
        """A VIDEO member of a tag block plays its full natural length
        (``play_to_end`` True), while image members in the same block keep
        the block's dwell time (``play_to_end`` False).  The block owns one
        dwell time, but a video shouldn't be truncated to it."""
        from datetime import datetime, timedelta, timezone

        from cms.services.slideshow_resolver import plan_slideshow

        profile = await _seed_profile(db_session, "phybvid")
        tag = await _seed_tag(db_session, "promoVid")
        img = await _seed_image(db_session, filename="still.png")
        vid = await _seed_video(db_session, filename="clip.mp4", duration=30.0)
        await _seed_variant(db_session, source=img, profile=profile, ext="jpg")
        await _seed_variant(db_session, source=vid, profile=profile, ext="mp4")
        base = datetime(2026, 6, 1, tzinfo=timezone.utc)
        await _tag_asset(db_session, asset=img, tag=tag, when=base)
        await _tag_asset(
            db_session, asset=vid, tag=tag, when=base + timedelta(seconds=5)
        )
        ss = await _seed_hybrid_slideshow(
            db_session, name="hybvid",
            entries=[{"tag": tag, "duration_ms": 8000}],
            checksum="hbv",
        )
        plan = await plan_slideshow(ss, profile.id, db_session)
        assert plan.ready
        by_name = {s.source_filename: s for s in plan.slides}
        # Image member: clipped to the block's 8s dwell.
        assert by_name["still.png"].play_to_end is False
        # Video member: plays its full natural length.
        assert by_name["clip.mp4"].play_to_end is True

    async def test_member_transition_applies_to_rest_only(self, db_session):
        """When ``member_transition`` is set, member 0 keeps the block's
        own ``transition`` (the transition INTO the block) while members
        1..N use ``member_transition``/``member_transition_ms``."""
        from datetime import datetime, timedelta, timezone

        from cms.services.slideshow_resolver import plan_slideshow

        profile = await _seed_profile(db_session, "pmt1")
        tag = await _seed_tag(db_session, "promoMT1")
        imgs = []
        for n in ("a", "b", "c"):
            img = await _seed_image(db_session, filename=f"{n}.png")
            await _seed_variant(db_session, source=img, profile=profile, ext="jpg")
            imgs.append(img)
        base = datetime(2026, 6, 3, tzinfo=timezone.utc)
        for k, img in enumerate(imgs):
            await _tag_asset(
                db_session, asset=img, tag=tag, when=base + timedelta(seconds=k)
            )
        ss = await _seed_hybrid_slideshow(
            db_session, name="mt1",
            entries=[{
                "tag": tag, "duration_ms": 5000,
                "transition": "fade", "transition_ms": 600,
                "member_transition": "wipe", "member_transition_ms": 250,
            }],
            checksum="mt1",
        )
        plan = await plan_slideshow(ss, profile.id, db_session)
        assert plan.ready
        # member 0 = block transition; members 1..N = member_transition.
        assert [s.transition for s in plan.slides] == ["fade", "wipe", "wipe"]
        assert [s.transition_ms for s in plan.slides] == [600, 250, 250]

    async def test_member_transition_null_inherits_block(self, db_session):
        """A NULL ``member_transition`` resolves byte-identically to the
        pre-feature behaviour: every member shares the block transition."""
        from datetime import datetime, timedelta, timezone

        from cms.services.slideshow_resolver import plan_slideshow

        profile = await _seed_profile(db_session, "pmt2")
        tag = await _seed_tag(db_session, "promoMT2")
        imgs = []
        for n in ("p", "q"):
            img = await _seed_image(db_session, filename=f"{n}.png")
            await _seed_variant(db_session, source=img, profile=profile, ext="jpg")
            imgs.append(img)
        base = datetime(2026, 6, 4, tzinfo=timezone.utc)
        for k, img in enumerate(imgs):
            await _tag_asset(
                db_session, asset=img, tag=tag, when=base + timedelta(seconds=k)
            )
        ss = await _seed_hybrid_slideshow(
            db_session, name="mt2",
            entries=[{
                "tag": tag, "duration_ms": 5000,
                "transition": "dissolve", "transition_ms": 800,
                # member_transition left unset -> NULL -> inherit.
            }],
            checksum="mt2",
        )
        plan = await plan_slideshow(ss, profile.id, db_session)
        assert plan.ready
        assert all(s.transition == "dissolve" for s in plan.slides)
        assert all(s.transition_ms == 800 for s in plan.slides)

        """asset, tag(block of 2), asset -> a single contiguous position
        sequence with the tag members spliced in the middle."""
        from datetime import datetime, timedelta, timezone

        from cms.services.slideshow_resolver import plan_slideshow

        profile = await _seed_profile(db_session, "phyb3")
        tag = await _seed_tag(db_session, "promoH3")
        head = await _seed_image(db_session, filename="head.png")
        tail = await _seed_image(db_session, filename="tail.png")
        m1 = await _seed_image(db_session, filename="m1.png")
        m2 = await _seed_image(db_session, filename="m2.png")
        for img in (head, tail, m1, m2):
            await _seed_variant(db_session, source=img, profile=profile, ext="jpg")
        base = datetime(2026, 6, 2, tzinfo=timezone.utc)
        await _tag_asset(db_session, asset=m1, tag=tag, when=base)
        await _tag_asset(db_session, asset=m2, tag=tag, when=base + timedelta(seconds=5))
        ss = await _seed_hybrid_slideshow(
            db_session, name="hyb3",
            entries=[
                {"asset": head, "duration_ms": 3000},
                {"tag": tag, "duration_ms": 5000},
                {"asset": tail, "duration_ms": 9000},
            ],
            checksum="hb3",
        )
        plan = await plan_slideshow(ss, profile.id, db_session)
        assert plan.ready
        assert [s.source_filename for s in plan.slides] == [
            "head.png", "m1.png", "m2.png", "tail.png",
        ]
        assert [s.position for s in plan.slides] == [0, 1, 2, 3]
        # Static slides keep their own durations; tag members inherit 5000.
        assert [s.duration_ms for s in plan.slides] == [3000, 5000, 5000, 9000]

    async def test_static_plus_tag_member_dedup_plays_twice(self, db_session):
        """A static slide for asset X and a tag block also containing X
        intentionally yields X twice (no dedup in v1)."""
        from datetime import datetime, timezone

        from cms.services.slideshow_resolver import plan_slideshow

        profile = await _seed_profile(db_session, "phyb4")
        tag = await _seed_tag(db_session, "promoH4")
        dup = await _seed_image(db_session, filename="dup.png")
        await _seed_variant(db_session, source=dup, profile=profile, ext="jpg")
        await _tag_asset(
            db_session, asset=dup, tag=tag,
            when=datetime(2026, 6, 3, tzinfo=timezone.utc),
        )
        ss = await _seed_hybrid_slideshow(
            db_session, name="hyb4",
            entries=[
                {"asset": dup, "duration_ms": 4000},
                {"tag": tag, "duration_ms": 5000},
            ],
            checksum="hb4",
        )
        plan = await plan_slideshow(ss, profile.id, db_session)
        assert plan.ready
        assert [s.source_filename for s in plan.slides] == ["dup.png", "dup.png"]
        assert [s.position for s in plan.slides] == [0, 1]

    async def test_persisted_slideshow_anchor_emitted_verbatim(self, db_session, app):
        """``assets.slideshow_anchor_at`` is emitted verbatim as
        ``started_at`` for a hybrid deck (no flooring), so tag-block growth
        leaves the on-screen slide stable."""
        from datetime import datetime, timezone

        from cms.services.slideshow_resolver import (
            _format_anchor,
            build_fetch_for_slideshow,
        )

        profile = await _seed_profile(db_session, "phyb5")
        group = await _seed_group(db_session, "ghyb5")
        device = await _seed_device(
            db_session, did="dhyb5", group=group, profile=profile,
        )
        tag = await _seed_tag(db_session, "promoH5")
        img = await _seed_image(db_session, filename="anc.png")
        await _seed_variant(
            db_session, source=img, profile=profile, checksum="ancv", ext="jpg",
        )
        await _tag_asset(
            db_session, asset=img, tag=tag,
            when=datetime(2026, 6, 4, tzinfo=timezone.utc),
        )
        anchor = datetime(2026, 6, 4, 9, 30, 0, tzinfo=timezone.utc)
        ss = await _seed_hybrid_slideshow(
            db_session, name="hyb5",
            entries=[{"tag": tag, "duration_ms": 5000}],
            checksum="hb5", anchor_at=anchor,
        )
        fetch = await build_fetch_for_slideshow(
            ss, device, "https://cms.example", db_session,
        )
        assert fetch is not None
        assert fetch.started_at == _format_anchor(anchor)

    async def test_null_slideshow_anchor_falls_back_to_floored(self, db_session, app):
        """A hybrid deck with NULL ``slideshow_anchor_at`` floors ``now`` to
        a cycle boundary (classic manual-slideshow behaviour)."""
        from datetime import datetime

        from cms.services.slideshow_resolver import build_fetch_for_slideshow

        profile = await _seed_profile(db_session, "phyb6")
        group = await _seed_group(db_session, "ghyb6")
        device = await _seed_device(
            db_session, did="dhyb6", group=group, profile=profile,
        )
        a = await _seed_image(db_session, filename="floorhyb.png")
        await _seed_variant(
            db_session, source=a, profile=profile, checksum="fhv", ext="jpg",
        )
        ss = await _seed_hybrid_slideshow(
            db_session, name="hyb6",
            entries=[{"asset": a, "duration_ms": 5000}],
            checksum="hb6",
        )
        fetch = await build_fetch_for_slideshow(
            ss, device, "https://cms.example", db_session,
        )
        assert fetch is not None
        anchor = datetime.fromisoformat(fetch.started_at.replace("Z", "+00:00"))
        assert int(anchor.timestamp() * 1000) % fetch.cycle_duration_ms == 0
