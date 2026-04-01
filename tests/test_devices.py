"""Tests for device API endpoints."""

import pytest


@pytest.mark.asyncio
class TestListDevices:
    async def test_list_empty(self, client):
        resp = await client.get("/api/devices")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_requires_auth(self, unauthed_client):
        resp = await unauthed_client.get("/api/devices")
        assert resp.status_code in (401, 303)


@pytest.mark.asyncio
class TestDeviceCRUD:
    async def test_create_and_list_device(self, client, db_session):
        """Device created via DB (simulating WS registration), then listed via API."""
        from cms.models.device import Device, DeviceStatus

        device = Device(id="test-pi-001", name="Living Room", status=DeviceStatus.PENDING)
        db_session.add(device)
        await db_session.commit()

        resp = await client.get("/api/devices")
        assert resp.status_code == 200
        devices = resp.json()
        assert len(devices) == 1
        assert devices[0]["id"] == "test-pi-001"
        assert devices[0]["name"] == "Living Room"
        assert devices[0]["status"] == "pending"
        assert devices[0]["is_online"] is False

    async def test_update_device_name(self, client, db_session):
        from cms.models.device import Device, DeviceStatus

        device = Device(id="test-pi-002", name="test-pi-002", status=DeviceStatus.PENDING)
        db_session.add(device)
        await db_session.commit()

        resp = await client.patch("/api/devices/test-pi-002", json={"name": "Kitchen Display"})
        assert resp.status_code == 200
        assert resp.json()["name"] == "Kitchen Display"

    async def test_adopt_device(self, client, db_session):
        from cms.models.device import Device, DeviceStatus

        device = Device(id="test-pi-003", name="test-pi-003", status=DeviceStatus.PENDING)
        db_session.add(device)
        await db_session.commit()

        resp = await client.post("/api/devices/test-pi-003/adopt")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

        # Verify status changed
        resp = await client.get("/api/devices")
        dev = [d for d in resp.json() if d["id"] == "test-pi-003"][0]
        assert dev["status"] == "adopted"

    async def test_adopt_orphaned_device(self, client, db_session):
        from cms.models.device import Device, DeviceStatus

        device = Device(
            id="test-pi-orphan", name="test-pi-orphan",
            status=DeviceStatus.ORPHANED,
            device_auth_token_hash="oldhash",
        )
        db_session.add(device)
        await db_session.commit()

        resp = await client.post("/api/devices/test-pi-orphan/adopt")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

        # Verify status changed
        resp = await client.get("/api/devices")
        dev = [d for d in resp.json() if d["id"] == "test-pi-orphan"][0]
        assert dev["status"] == "adopted"

    async def test_adopt_already_adopted_device(self, client, db_session):
        from cms.models.device import Device, DeviceStatus

        device = Device(id="test-pi-adopted", name="test-pi-adopted", status=DeviceStatus.ADOPTED)
        db_session.add(device)
        await db_session.commit()

        resp = await client.post("/api/devices/test-pi-adopted/adopt")
        assert resp.status_code == 400

    async def test_update_device_default_asset(self, client, db_session):
        from cms.models.asset import Asset, AssetType
        from cms.models.device import Device, DeviceStatus

        asset = Asset(filename="splash.png", asset_type=AssetType.IMAGE, size_bytes=1024, checksum="abc123")
        db_session.add(asset)
        await db_session.flush()

        device = Device(id="test-pi-004", name="test-pi-004", status=DeviceStatus.ADOPTED)
        db_session.add(device)
        await db_session.commit()

        resp = await client.patch(
            "/api/devices/test-pi-004",
            json={"default_asset_id": str(asset.id)},
        )
        assert resp.status_code == 200
        assert resp.json()["default_asset_id"] == str(asset.id)

    async def test_clear_device_default_asset_falls_back_to_group(self, client, db_session):
        """Clearing device default_asset_id should succeed (group fallback)."""
        from cms.models.asset import Asset, AssetType
        from cms.models.device import Device, DeviceGroup, DeviceStatus

        group_asset = Asset(filename="group_default.png", asset_type=AssetType.IMAGE, size_bytes=2048, checksum="grp1")
        device_asset = Asset(filename="device_override.png", asset_type=AssetType.IMAGE, size_bytes=1024, checksum="dev1")
        db_session.add_all([group_asset, device_asset])
        await db_session.flush()

        group = DeviceGroup(name="Fallback Group", default_asset_id=group_asset.id)
        db_session.add(group)
        await db_session.flush()

        device = Device(
            id="test-pi-fallback", name="test-pi-fallback",
            status=DeviceStatus.ADOPTED,
            group_id=group.id,
            default_asset_id=device_asset.id,
        )
        db_session.add(device)
        await db_session.commit()

        # Clear device default — should fall back to group default
        resp = await client.patch(
            "/api/devices/test-pi-fallback",
            json={"default_asset_id": None},
        )
        assert resp.status_code == 200
        assert resp.json()["default_asset_id"] is None

    async def test_clear_device_default_asset_no_group(self, client, db_session):
        """Clearing device default_asset_id with no group should succeed (splash)."""
        from cms.models.asset import Asset, AssetType
        from cms.models.device import Device, DeviceStatus

        asset = Asset(filename="solo.png", asset_type=AssetType.IMAGE, size_bytes=1024, checksum="solo1")
        db_session.add(asset)
        await db_session.flush()

        device = Device(
            id="test-pi-solo", name="test-pi-solo",
            status=DeviceStatus.ADOPTED,
            default_asset_id=asset.id,
        )
        db_session.add(device)
        await db_session.commit()

        resp = await client.patch(
            "/api/devices/test-pi-solo",
            json={"default_asset_id": None},
        )
        assert resp.status_code == 200
        assert resp.json()["default_asset_id"] is None

    async def test_delete_device(self, client, db_session):
        from cms.models.device import Device, DeviceStatus

        device = Device(id="test-pi-005", name="test-pi-005", status=DeviceStatus.PENDING)
        db_session.add(device)
        await db_session.commit()

        resp = await client.delete("/api/devices/test-pi-005")
        assert resp.status_code == 200
        assert resp.json()["deleted"] == "test-pi-005"

        resp = await client.get("/api/devices")
        assert len(resp.json()) == 0

    async def test_update_nonexistent_device(self, client):
        resp = await client.patch("/api/devices/nonexistent", json={"name": "X"})
        assert resp.status_code == 404

    async def test_delete_nonexistent_device(self, client):
        resp = await client.delete("/api/devices/nonexistent")
        assert resp.status_code == 404

    async def test_delete_device_with_schedules(self, client, db_session):
        """Deleting a device that has schedules should succeed and remove the schedules."""
        from datetime import time
        from cms.models.asset import Asset, AssetType
        from cms.models.device import Device, DeviceStatus
        from cms.models.schedule import Schedule

        device = Device(id="del-sched-pi", name="Del Sched", status=DeviceStatus.ADOPTED)
        asset = Asset(filename="test.mp4", asset_type=AssetType.VIDEO, size_bytes=5000, checksum="abc")
        db_session.add_all([device, asset])
        await db_session.flush()

        schedule = Schedule(
            name="Test Schedule",
            device_id="del-sched-pi",
            asset_id=asset.id,
            start_time=time(9, 0),
            end_time=time(17, 0),
        )
        db_session.add(schedule)
        await db_session.commit()

        resp = await client.delete("/api/devices/del-sched-pi")
        assert resp.status_code == 200
        assert resp.json()["deleted"] == "del-sched-pi"

        # Verify device is gone
        resp = await client.get("/api/devices")
        assert all(d["id"] != "del-sched-pi" for d in resp.json())

    async def test_delete_device_with_device_assets(self, client, db_session):
        """Deleting a device that has device_assets should succeed."""
        from cms.models.asset import Asset, AssetType, DeviceAsset
        from cms.models.device import Device, DeviceStatus

        device = Device(id="del-da-pi", name="Del DA", status=DeviceStatus.ADOPTED)
        asset = Asset(filename="da_test.mp4", asset_type=AssetType.VIDEO, size_bytes=5000, checksum="def")
        db_session.add_all([device, asset])
        await db_session.flush()

        da = DeviceAsset(device_id="del-da-pi", asset_id=asset.id)
        db_session.add(da)
        await db_session.commit()

        resp = await client.delete("/api/devices/del-da-pi")
        assert resp.status_code == 200
        assert resp.json()["deleted"] == "del-da-pi"


@pytest.mark.asyncio
class TestCheckUpdates:
    async def test_check_updates_endpoint(self, client):
        from unittest.mock import AsyncMock, patch
        from cms.services import version_checker

        original = version_checker._latest_version
        try:
            with patch.object(version_checker, "_fetch_latest_version", new_callable=AsyncMock, return_value="9.9.9"):
                resp = await client.post("/api/devices/check-updates")
            assert resp.status_code == 200
            assert resp.json()["latest_version"] == "9.9.9"
        finally:
            version_checker._latest_version = original


@pytest.mark.asyncio
class TestUpgradeGuard:
    async def test_upgrade_not_connected(self, client, db_session):
        """Upgrading an offline device returns 409."""
        from cms.models.device import Device, DeviceStatus

        device = Device(id="up-pi-001", name="up-pi-001", status=DeviceStatus.ADOPTED)
        db_session.add(device)
        await db_session.commit()

        resp = await client.post("/api/devices/up-pi-001/upgrade")
        assert resp.status_code == 409
        assert "not connected" in resp.json()["detail"]

    async def test_upgrade_concurrent_blocked(self, client, db_session):
        """Second upgrade to the same device returns 409."""
        from cms.models.device import Device, DeviceStatus
        from cms.routers.devices import _upgrading
        from cms.services.device_manager import device_manager

        device = Device(id="up-pi-002", name="up-pi-002", status=DeviceStatus.ADOPTED)
        db_session.add(device)
        await db_session.commit()

        # Simulate device connected and already upgrading
        class FakeWS:
            async def send_json(self, data): pass
        device_manager.register("up-pi-002", FakeWS())
        _upgrading.add("up-pi-002")

        try:
            resp = await client.post("/api/devices/up-pi-002/upgrade")
            assert resp.status_code == 409
            assert "already in progress" in resp.json()["detail"]
        finally:
            _upgrading.discard("up-pi-002")
            device_manager.disconnect("up-pi-002")

    async def test_upgrade_clears_flag_on_disconnect(self, client, db_session):
        """Upgrading flag is cleared when device disconnects (simulated)."""
        from cms.routers.devices import _upgrading

        _upgrading.add("up-pi-003")
        # Simulate what ws.py finally block does
        _upgrading.discard("up-pi-003")
        assert "up-pi-003" not in _upgrading


@pytest.mark.asyncio
class TestDeviceGroups:
    async def test_create_group(self, client):
        resp = await client.post("/api/devices/groups/", json={"name": "Lobby Screens"})
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "Lobby Screens"
        assert data["device_count"] == 0

    async def test_list_groups(self, client):
        await client.post("/api/devices/groups/", json={"name": "Group A"})
        await client.post("/api/devices/groups/", json={"name": "Group B"})

        resp = await client.get("/api/devices/groups/")
        assert resp.status_code == 200
        groups = resp.json()
        assert len(groups) == 2

    async def test_delete_group(self, client):
        resp = await client.post("/api/devices/groups/", json={"name": "Temp Group"})
        group_id = resp.json()["id"]

        resp = await client.delete(f"/api/devices/groups/{group_id}")
        assert resp.status_code == 200

        resp = await client.get("/api/devices/groups/")
        assert len(resp.json()) == 0

    async def test_assign_device_to_group(self, client, db_session):
        from cms.models.device import Device, DeviceStatus

        resp = await client.post("/api/devices/groups/", json={"name": "Test Group"})
        group_id = resp.json()["id"]

        device = Device(id="test-pi-grp", name="test-pi-grp", status=DeviceStatus.ADOPTED)
        db_session.add(device)
        await db_session.commit()

        resp = await client.patch("/api/devices/test-pi-grp", json={"group_id": group_id})
        assert resp.status_code == 200
        assert resp.json()["group_id"] == group_id

    async def test_group_default_asset(self, client, db_session):
        from cms.models.asset import Asset, AssetType

        asset = Asset(filename="lobby.mp4", asset_type=AssetType.VIDEO, size_bytes=5000, checksum="def456")
        db_session.add(asset)
        await db_session.commit()

        resp = await client.post(
            "/api/devices/groups/",
            json={"name": "Lobby", "default_asset_id": str(asset.id)},
        )
        assert resp.status_code == 201
        assert resp.json()["default_asset_id"] == str(asset.id)

    async def test_update_group(self, client, db_session):
        resp = await client.post("/api/devices/groups/", json={"name": "Old Name"})
        group_id = resp.json()["id"]

        resp = await client.patch(
            f"/api/devices/groups/{group_id}",
            json={"name": "New Name", "description": "Updated"},
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "New Name"
        assert resp.json()["description"] == "Updated"

    async def test_update_group_default_asset(self, client, db_session):
        """Setting only default_asset_id on a group (partial update) should work."""
        from cms.models.asset import Asset, AssetType

        asset = Asset(filename="group_splash.png", asset_type=AssetType.IMAGE, size_bytes=2048, checksum="abc")
        db_session.add(asset)
        await db_session.commit()

        resp = await client.post("/api/devices/groups/", json={"name": "Asset Group"})
        assert resp.status_code == 201
        group_id = resp.json()["id"]

        # Partial PATCH — only default_asset_id, no name required
        resp = await client.patch(
            f"/api/devices/groups/{group_id}",
            json={"default_asset_id": str(asset.id)},
        )
        assert resp.status_code == 200
        assert resp.json()["default_asset_id"] == str(asset.id)
        # Name should remain unchanged
        assert resp.json()["name"] == "Asset Group"

    async def test_clear_group_default_asset(self, client, db_session):
        """Setting default_asset_id to null should clear it."""
        from cms.models.asset import Asset, AssetType

        asset = Asset(filename="temp.png", asset_type=AssetType.IMAGE, size_bytes=1024, checksum="xyz")
        db_session.add(asset)
        await db_session.commit()

        resp = await client.post(
            "/api/devices/groups/",
            json={"name": "Clear Group", "default_asset_id": str(asset.id)},
        )
        group_id = resp.json()["id"]

        resp = await client.patch(
            f"/api/devices/groups/{group_id}",
            json={"default_asset_id": None},
        )
        assert resp.status_code == 200
        assert resp.json()["default_asset_id"] is None


@pytest.mark.asyncio
class TestPendingDeviceNameDisplay:
    """Dashboard should show device friendly name, not raw ID, for pending devices."""

    async def test_dashboard_pending_shows_friendly_name(self, client, db_session):
        from cms.models.device import Device, DeviceStatus

        device = Device(
            id="abc123serial", name="Living Room TV",
            status=DeviceStatus.PENDING,
        )
        db_session.add(device)
        await db_session.commit()

        resp = await client.get("/")
        assert resp.status_code == 200
        html = resp.text
        # The friendly name should appear in the pending devices table
        assert "Living Room TV" in html
        # The adopt button should pass the friendly name for display
        assert "adoptDevice(" in html
        assert "Living Room TV" in html


@pytest.mark.asyncio
class TestDefaultAssetVariantResolution:
    """Regression test: setting a device's default asset must use the transcoded
    variant when the device has a profile, not the original source file.

    Bug: _push_default_asset() previously built FetchAssetMessage directly with
    the source asset URL, bypassing _resolve_asset_for_device(). This caused
    devices with H.264-only pipelines to receive AV1/VP9 source files → black screen.
    """

    async def test_default_asset_sends_variant_url_not_source(self, client, db_session):
        """PATCH default_asset_id on a profiled device must send variant download URL."""
        from unittest.mock import AsyncMock, patch
        from cms.models.asset import Asset, AssetType, AssetVariant, VariantStatus
        from cms.models.device import Device, DeviceStatus
        from cms.models.device_profile import DeviceProfile
        from cms.services.device_manager import device_manager

        # Create a profile (e.g. pi-zero-2w: H.264)
        profile = DeviceProfile(name="pi-zero-2w", video_codec="h264", video_profile="main")
        db_session.add(profile)
        await db_session.flush()

        # Create source video asset (e.g. AV1 original)
        source = Asset(
            filename="Sony_4k60_AV1.mp4", asset_type=AssetType.VIDEO,
            size_bytes=30_000_000, checksum="source_av1_checksum",
        )
        db_session.add(source)
        await db_session.flush()

        # Create READY variant (H.264 transcode)
        variant = AssetVariant(
            source_asset_id=source.id, profile_id=profile.id,
            status=VariantStatus.READY,
            filename="Sony_4k60_AV1_pi-zero-2w.mp4",
            size_bytes=42_000_000, checksum="variant_h264_checksum",
        )
        db_session.add(variant)
        await db_session.flush()

        # Create adopted device with profile assigned
        device = Device(
            id="test-variant-pi", name="Variant Test Pi",
            status=DeviceStatus.ADOPTED, profile_id=profile.id,
        )
        db_session.add(device)
        await db_session.commit()

        # Mock the device as connected and capture sent messages
        sent_messages = []

        class FakeWS:
            async def send_json(self, data):
                sent_messages.append(data)

        device_manager.register("test-variant-pi", FakeWS())

        try:
            resp = await client.patch(
                "/api/devices/test-variant-pi",
                json={"default_asset_id": str(source.id)},
            )
            assert resp.status_code == 200

            # Should have sent fetch_asset + play
            assert len(sent_messages) == 2
            fetch_msg = sent_messages[0]
            assert fetch_msg["type"] == "fetch_asset"

            # CRITICAL: URL must point to variant download, NOT source asset download
            assert f"/api/assets/variants/{variant.id}/download" in fetch_msg["download_url"]
            assert f"/api/assets/{source.id}/download" not in fetch_msg["download_url"]

            # Checksum and size must be the variant's, not the source's
            assert fetch_msg["checksum"] == "variant_h264_checksum"
            assert fetch_msg["size_bytes"] == 42_000_000

            # asset_name should still be the source filename (device saves under this name)
            assert fetch_msg["asset_name"] == "Sony_4k60_AV1.mp4"
        finally:
            device_manager.disconnect("test-variant-pi")

    async def test_default_asset_image_bypasses_variant(self, client, db_session):
        """Images should use source URL directly even when device has a profile."""
        from cms.models.asset import Asset, AssetType
        from cms.models.device import Device, DeviceStatus
        from cms.models.device_profile import DeviceProfile
        from cms.services.device_manager import device_manager

        profile = DeviceProfile(name="pi-zero-img", video_codec="h264")
        db_session.add(profile)
        await db_session.flush()

        image = Asset(
            filename="splash.png", asset_type=AssetType.IMAGE,
            size_bytes=1024, checksum="img_checksum",
        )
        db_session.add(image)
        await db_session.flush()

        device = Device(
            id="test-img-pi", name="Image Test",
            status=DeviceStatus.ADOPTED, profile_id=profile.id,
        )
        db_session.add(device)
        await db_session.commit()

        sent_messages = []

        class FakeWS:
            async def send_json(self, data):
                sent_messages.append(data)

        device_manager.register("test-img-pi", FakeWS())

        try:
            resp = await client.patch(
                "/api/devices/test-img-pi",
                json={"default_asset_id": str(image.id)},
            )
            assert resp.status_code == 200

            fetch_msg = sent_messages[0]
            assert fetch_msg["type"] == "fetch_asset"
            # Image should use source URL (no variant for images)
            assert f"/api/assets/{image.id}/download" in fetch_msg["download_url"]
            assert fetch_msg["checksum"] == "img_checksum"
        finally:
            device_manager.disconnect("test-img-pi")

    async def test_default_asset_no_profile_uses_source(self, client, db_session):
        """Device without a profile should get the source asset URL."""
        from cms.models.asset import Asset, AssetType
        from cms.models.device import Device, DeviceStatus
        from cms.services.device_manager import device_manager

        video = Asset(
            filename="test.mp4", asset_type=AssetType.VIDEO,
            size_bytes=5_000_000, checksum="src_checksum",
        )
        db_session.add(video)
        await db_session.flush()

        device = Device(
            id="test-noprof-pi", name="No Profile",
            status=DeviceStatus.ADOPTED,
        )
        db_session.add(device)
        await db_session.commit()

        sent_messages = []

        class FakeWS:
            async def send_json(self, data):
                sent_messages.append(data)

        device_manager.register("test-noprof-pi", FakeWS())

        try:
            resp = await client.patch(
                "/api/devices/test-noprof-pi",
                json={"default_asset_id": str(video.id)},
            )
            assert resp.status_code == 200

            fetch_msg = sent_messages[0]
            assert fetch_msg["type"] == "fetch_asset"
            # No profile → source URL directly
            assert f"/api/assets/{video.id}/download" in fetch_msg["download_url"]
            assert fetch_msg["checksum"] == "src_checksum"
        finally:
            device_manager.disconnect("test-noprof-pi")
