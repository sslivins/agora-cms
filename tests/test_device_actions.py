"""Tests for device action endpoints (factory reset, local API toggle, splash fix)."""

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from cms.models.device import Device, DeviceStatus
from cms.services.device_manager import DeviceManager, device_manager


@pytest.mark.asyncio
class TestFactoryReset:
    async def test_factory_reset_sends_message(self, client, db_session):
        device = Device(id="fr-001", name="Test Device", status=DeviceStatus.ADOPTED)
        db_session.add(device)
        await db_session.commit()

        ws = AsyncMock()
        device_manager.register("fr-001", ws)
        try:
            resp = await client.post("/api/devices/fr-001/factory-reset")
            assert resp.status_code == 200
            assert resp.json()["ok"] is True

            ws.send_json.assert_called_once()
            msg = ws.send_json.call_args[0][0]
            assert msg["type"] == "factory_reset"
            assert "protocol_version" in msg
        finally:
            device_manager.disconnect("fr-001")

    async def test_factory_reset_device_not_found(self, client):
        resp = await client.post("/api/devices/nonexistent/factory-reset")
        assert resp.status_code == 404

    async def test_factory_reset_device_not_connected(self, client, db_session):
        device = Device(id="fr-002", name="Offline", status=DeviceStatus.ADOPTED)
        db_session.add(device)
        await db_session.commit()

        resp = await client.post("/api/devices/fr-002/factory-reset")
        assert resp.status_code == 409

    async def test_factory_reset_requires_auth(self, unauthed_client, db_session):
        resp = await unauthed_client.post("/api/devices/fr-001/factory-reset")
        assert resp.status_code in (401, 303)


@pytest.mark.asyncio
class TestLocalApiToggle:
    async def test_disable_local_api(self, client, db_session):
        device = Device(id="la-001", name="Test", status=DeviceStatus.ADOPTED)
        db_session.add(device)
        await db_session.commit()

        ws = AsyncMock()
        device_manager.register("la-001", ws)
        try:
            resp = await client.post("/api/devices/la-001/local-api", json={"enabled": False})
            assert resp.status_code == 200
            assert resp.json()["ok"] is True

            ws.send_json.assert_called_once()
            msg = ws.send_json.call_args[0][0]
            assert msg["type"] == "config"
            assert msg["local_api_enabled"] is False

            conn = device_manager.get("la-001")
            assert conn.local_api_enabled is False
        finally:
            device_manager.disconnect("la-001")

    async def test_enable_local_api(self, client, db_session):
        device = Device(id="la-002", name="Test", status=DeviceStatus.ADOPTED)
        db_session.add(device)
        await db_session.commit()

        ws = AsyncMock()
        device_manager.register("la-002", ws)
        try:
            resp = await client.post("/api/devices/la-002/local-api", json={"enabled": True})
            assert resp.status_code == 200

            msg = ws.send_json.call_args[0][0]
            assert msg["local_api_enabled"] is True

            conn = device_manager.get("la-002")
            assert conn.local_api_enabled is True
        finally:
            device_manager.disconnect("la-002")

    async def test_invalid_enabled_value(self, client, db_session):
        device = Device(id="la-003", name="Test", status=DeviceStatus.ADOPTED)
        db_session.add(device)
        await db_session.commit()

        resp = await client.post("/api/devices/la-003/local-api", json={"enabled": "yes"})
        assert resp.status_code == 400

    async def test_device_not_connected(self, client, db_session):
        device = Device(id="la-004", name="Offline", status=DeviceStatus.ADOPTED)
        db_session.add(device)
        await db_session.commit()

        resp = await client.post("/api/devices/la-004/local-api", json={"enabled": False})
        assert resp.status_code == 409

    async def test_requires_auth(self, unauthed_client):
        resp = await unauthed_client.post("/api/devices/la-001/local-api", json={"enabled": False})
        assert resp.status_code in (401, 303)


class TestDeviceManagerLocalApi:
    def test_local_api_enabled_default(self):
        dm = DeviceManager()
        ws = AsyncMock()
        conn = dm.register("dm-la-1", ws)
        assert conn.local_api_enabled is None

    def test_update_status_sets_local_api_enabled(self):
        dm = DeviceManager()
        ws = AsyncMock()
        dm.register("dm-la-2", ws)
        dm.update_status("dm-la-2", mode="splash", asset=None, local_api_enabled=True)
        conn = dm.get("dm-la-2")
        assert conn.local_api_enabled is True

    def test_local_api_enabled_in_get_all_states(self):
        dm = DeviceManager()
        ws = AsyncMock()
        dm.register("dm-la-3", ws)
        dm.update_status("dm-la-3", mode="splash", asset=None, local_api_enabled=False)
        states = {s["device_id"]: s for s in dm.get_all_states()}
        assert states["dm-la-3"]["local_api_enabled"] is False


@pytest.mark.asyncio
class TestSplashFix:
    """Verify that changing default asset pushes sync, not play."""

    async def test_push_default_asset_sends_sync_not_play(self, client, db_session, app):
        """When a device's default asset is changed, the CMS should send
        fetch_asset + sync (not play) to avoid the race condition."""
        from cms.models.asset import Asset
        from cms.models.device import Device, DeviceStatus

        device = Device(id="splash-001", name="Test", status=DeviceStatus.ADOPTED)
        db_session.add(device)

        asset_id = uuid.uuid4()
        asset = Asset(id=asset_id, filename="test.mp4", asset_type="video", size_bytes=1024, checksum="abc123")
        db_session.add(asset)
        await db_session.commit()

        ws = AsyncMock()
        device_manager.register("splash-001", ws)
        try:
            with patch("cms.routers.devices.push_sync_to_device", new_callable=AsyncMock) as mock_sync:
                resp = await client.patch(
                    "/api/devices/splash-001",
                    json={"default_asset_id": str(asset_id)},
                )
                assert resp.status_code == 200

                # push_sync_to_device should have been called
                mock_sync.assert_called()

                # No PlayMessage should have been sent — check all calls
                for call in ws.send_json.call_args_list:
                    msg = call[0][0]
                    assert msg.get("type") != "play", "Play message should not be sent for default asset"
        finally:
            device_manager.disconnect("splash-001")

    async def test_clear_default_asset_sends_sync(self, client, db_session):
        """Clearing a device's default asset should push a full sync."""
        device = Device(
            id="splash-002", name="Test", status=DeviceStatus.ADOPTED,
            default_asset_id=None,
        )
        db_session.add(device)
        await db_session.commit()

        ws = AsyncMock()
        device_manager.register("splash-002", ws)
        try:
            with patch("cms.routers.devices.push_sync_to_device", new_callable=AsyncMock) as mock_sync:
                resp = await client.patch(
                    "/api/devices/splash-002",
                    json={"default_asset_id": None},
                )
                assert resp.status_code == 200
                # Should push sync for "no default" case too
                mock_sync.assert_called()
        finally:
            device_manager.disconnect("splash-002")
