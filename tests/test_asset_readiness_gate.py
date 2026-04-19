"""Endpoint tests for the asset-readiness gate (issue #201).

Covers the PATCH/POST guards on:
  * PATCH /api/devices/{id}               (default_asset_id)
  * POST  /api/devices/groups/            (default_asset_id)
  * PATCH /api/devices/groups/{id}        (default_asset_id)
  * POST  /api/schedules                  (asset_id)
  * PATCH /api/schedules/{id}             (asset_id)

Rule: an asset is selectable iff every profile with any live (non-deleted)
variant has at least one variant in ``VariantStatus.READY``.
"""

from __future__ import annotations

import pytest


async def _ready_asset(db_session, *, filename: str = "ready.mp4"):
    """Create an asset with a single READY variant → selectable."""
    from cms.models.asset import Asset, AssetType, AssetVariant, VariantStatus
    from cms.models.device_profile import DeviceProfile

    asset = Asset(
        filename=filename,
        asset_type=AssetType.VIDEO,
        size_bytes=100,
        checksum=f"ready-{filename}",
    )
    profile = DeviceProfile(name=f"profile-{filename}")
    db_session.add_all([asset, profile])
    await db_session.flush()
    db_session.add(
        AssetVariant(
            source_asset_id=asset.id,
            profile_id=profile.id,
            filename=f"{asset.id}.mp4",
            status=VariantStatus.READY,
        )
    )
    await db_session.commit()
    return asset


async def _in_flight_asset(db_session, *, filename: str = "wip.mp4"):
    """Create an asset whose lone variant is PROCESSING → NOT selectable."""
    from cms.models.asset import Asset, AssetType, AssetVariant, VariantStatus
    from cms.models.device_profile import DeviceProfile

    asset = Asset(
        filename=filename,
        asset_type=AssetType.VIDEO,
        size_bytes=100,
        checksum=f"wip-{filename}",
    )
    profile = DeviceProfile(name=f"profile-{filename}")
    db_session.add_all([asset, profile])
    await db_session.flush()
    db_session.add(
        AssetVariant(
            source_asset_id=asset.id,
            profile_id=profile.id,
            filename=f"{asset.id}.mp4",
            status=VariantStatus.PROCESSING,
            progress=45.0,
        )
    )
    await db_session.commit()
    return asset


async def _failed_asset(db_session, *, filename: str = "bad.mp4"):
    """Create an asset whose lone variant is FAILED → NOT selectable."""
    from cms.models.asset import Asset, AssetType, AssetVariant, VariantStatus
    from cms.models.device_profile import DeviceProfile

    asset = Asset(
        filename=filename,
        asset_type=AssetType.VIDEO,
        size_bytes=100,
        checksum=f"fail-{filename}",
    )
    profile = DeviceProfile(name=f"profile-{filename}")
    db_session.add_all([asset, profile])
    await db_session.flush()
    db_session.add(
        AssetVariant(
            source_asset_id=asset.id,
            profile_id=profile.id,
            filename=f"{asset.id}.mp4",
            status=VariantStatus.FAILED,
            error_message="ffmpeg died",
        )
    )
    await db_session.commit()
    return asset


@pytest.mark.asyncio
class TestDeviceSplashReadinessGate:
    async def test_patch_device_rejects_in_flight_asset(self, client, db_session):
        from cms.models.device import Device, DeviceStatus

        asset = await _in_flight_asset(db_session)
        device = Device(id="readiness-pi", name="readiness-pi", status=DeviceStatus.ADOPTED)
        db_session.add(device)
        await db_session.commit()

        resp = await client.patch(
            "/api/devices/readiness-pi",
            json={"default_asset_id": str(asset.id)},
        )
        assert resp.status_code == 422
        assert "transcoding" in resp.json()["detail"].lower()

    async def test_patch_device_rejects_failed_asset(self, client, db_session):
        from cms.models.device import Device, DeviceStatus

        asset = await _failed_asset(db_session)
        device = Device(id="readiness-pi-2", name="readiness-pi-2", status=DeviceStatus.ADOPTED)
        db_session.add(device)
        await db_session.commit()

        resp = await client.patch(
            "/api/devices/readiness-pi-2",
            json={"default_asset_id": str(asset.id)},
        )
        assert resp.status_code == 422
        assert "failed" in resp.json()["detail"].lower()

    async def test_patch_device_accepts_ready_asset(self, client, db_session):
        from cms.models.device import Device, DeviceStatus

        asset = await _ready_asset(db_session)
        device = Device(id="readiness-pi-3", name="readiness-pi-3", status=DeviceStatus.ADOPTED)
        db_session.add(device)
        await db_session.commit()

        resp = await client.patch(
            "/api/devices/readiness-pi-3",
            json={"default_asset_id": str(asset.id)},
        )
        assert resp.status_code == 200
        assert resp.json()["default_asset_id"] == str(asset.id)


@pytest.mark.asyncio
class TestGroupSplashReadinessGate:
    async def test_create_group_rejects_in_flight_asset(self, client, db_session):
        asset = await _in_flight_asset(db_session, filename="grp-wip.mp4")

        resp = await client.post(
            "/api/devices/groups/",
            json={"name": "Bad Group", "default_asset_id": str(asset.id)},
        )
        assert resp.status_code == 422
        assert "transcoding" in resp.json()["detail"].lower()

    async def test_create_group_accepts_ready_asset(self, client, db_session):
        asset = await _ready_asset(db_session, filename="grp-ok.mp4")

        resp = await client.post(
            "/api/devices/groups/",
            json={"name": "Good Group", "default_asset_id": str(asset.id)},
        )
        assert resp.status_code == 201
        assert resp.json()["default_asset_id"] == str(asset.id)

    async def test_patch_group_rejects_in_flight_asset(self, client, db_session):
        from cms.models.device import DeviceGroup

        group = DeviceGroup(name="Patch Group")
        db_session.add(group)
        await db_session.commit()
        asset = await _in_flight_asset(db_session, filename="grp-patch-wip.mp4")

        resp = await client.patch(
            f"/api/devices/groups/{group.id}",
            json={"default_asset_id": str(asset.id)},
        )
        assert resp.status_code == 422

    async def test_patch_group_accepts_ready_asset(self, client, db_session):
        from cms.models.device import DeviceGroup

        group = DeviceGroup(name="Patch OK Group")
        db_session.add(group)
        await db_session.commit()
        asset = await _ready_asset(db_session, filename="grp-patch-ok.mp4")

        resp = await client.patch(
            f"/api/devices/groups/{group.id}",
            json={"default_asset_id": str(asset.id)},
        )
        assert resp.status_code == 200
        assert resp.json()["default_asset_id"] == str(asset.id)


@pytest.mark.asyncio
class TestScheduleAssetReadinessGate:
    async def _group(self, db_session, *, name="Sched RG"):
        from cms.models.device import DeviceGroup

        group = DeviceGroup(name=name)
        db_session.add(group)
        await db_session.commit()
        return group

    async def test_create_schedule_rejects_in_flight_asset(self, client, db_session):
        group = await self._group(db_session, name="Sched WIP")
        asset = await _in_flight_asset(db_session, filename="sched-wip.mp4")

        resp = await client.post(
            "/api/schedules",
            json={
                "name": "WIP Schedule",
                "group_id": str(group.id),
                "asset_id": str(asset.id),
                "start_time": "08:00",
                "end_time": "12:00",
            },
        )
        assert resp.status_code == 422
        assert "transcoding" in resp.json()["detail"].lower()

    async def test_create_schedule_rejects_failed_asset(self, client, db_session):
        group = await self._group(db_session, name="Sched Failed")
        asset = await _failed_asset(db_session, filename="sched-bad.mp4")

        resp = await client.post(
            "/api/schedules",
            json={
                "name": "Bad Schedule",
                "group_id": str(group.id),
                "asset_id": str(asset.id),
                "start_time": "08:00",
                "end_time": "12:00",
            },
        )
        assert resp.status_code == 422
        assert "failed" in resp.json()["detail"].lower()

    async def test_create_schedule_accepts_ready_asset(self, client, db_session):
        group = await self._group(db_session, name="Sched Good")
        asset = await _ready_asset(db_session, filename="sched-ok.mp4")

        resp = await client.post(
            "/api/schedules",
            json={
                "name": "OK Schedule",
                "group_id": str(group.id),
                "asset_id": str(asset.id),
                "start_time": "08:00",
                "end_time": "12:00",
            },
        )
        assert resp.status_code == 201

    async def test_patch_schedule_rejects_swap_to_in_flight_asset(
        self, client, db_session
    ):
        group = await self._group(db_session, name="Sched Swap")
        ready = await _ready_asset(db_session, filename="sched-swap-ok.mp4")
        wip = await _in_flight_asset(db_session, filename="sched-swap-wip.mp4")

        create = await client.post(
            "/api/schedules",
            json={
                "name": "Swap Schedule",
                "group_id": str(group.id),
                "asset_id": str(ready.id),
                "start_time": "08:00",
                "end_time": "12:00",
            },
        )
        sched_id = create.json()["id"]

        resp = await client.patch(
            f"/api/schedules/{sched_id}",
            json={"asset_id": str(wip.id)},
        )
        assert resp.status_code == 422

    async def test_patch_schedule_without_asset_change_still_works(
        self, client, db_session
    ):
        """Editing other fields on a schedule whose asset is now mid-transcode
        must still succeed — we only gate on new asset assignments."""
        from cms.models.asset import Asset, AssetType, AssetVariant, VariantStatus
        from cms.models.device import DeviceGroup
        from cms.models.device_profile import DeviceProfile

        group = DeviceGroup(name="Sched Preserve")
        asset = Asset(
            filename="sched-preserve.mp4",
            asset_type=AssetType.VIDEO,
            size_bytes=100,
            checksum="preserve1",
        )
        profile = DeviceProfile(name="profile-preserve")
        db_session.add_all([group, asset, profile])
        await db_session.flush()
        # Start it READY so the schedule can be created…
        variant = AssetVariant(
            source_asset_id=asset.id,
            profile_id=profile.id,
            filename=f"{asset.id}.mp4",
            status=VariantStatus.READY,
        )
        db_session.add(variant)
        await db_session.commit()

        create = await client.post(
            "/api/schedules",
            json={
                "name": "Preserve",
                "group_id": str(group.id),
                "asset_id": str(asset.id),
                "start_time": "08:00",
                "end_time": "12:00",
            },
        )
        assert create.status_code == 201
        sched_id = create.json()["id"]

        # …then simulate a re-transcode flipping the variant back to PROCESSING
        variant.status = VariantStatus.PROCESSING
        await db_session.commit()

        # Editing another field should still succeed — only asset swaps gate.
        resp = await client.patch(
            f"/api/schedules/{sched_id}",
            json={"priority": 7},
        )
        assert resp.status_code == 200
        assert resp.json()["priority"] == 7
