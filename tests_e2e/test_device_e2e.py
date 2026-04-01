"""End-to-end tests for the device ↔ CMS lifecycle.

Uses FakeDevice (WebSocket client) to simulate real devices connecting
to the CMS and validates the full registration, adoption, scheduling,
and command delivery flow.
"""

import asyncio
import threading
import time

import httpx
import pytest

from tests_e2e.conftest import run_async
from tests_e2e.fake_device import FakeDevice


def _login_cookies(base_url: str) -> dict:
    """Get session cookies by logging in with httpx."""
    with httpx.Client(base_url=base_url) as c:
        r = c.post(
            "/login",
            data={"username": "admin", "password": "testpass"},
            follow_redirects=False,
        )
        return dict(r.cookies)


class TestDeviceRegistration:
    """New device connects and registers with the CMS."""

    def test_new_device_registers_as_pending(self, api, ws_url, e2e_server):
        """A brand-new device should appear as 'pending' after connecting."""

        async def run():
            async with FakeDevice("reg-001", ws_url) as dev:
                assert dev.auth_token, "CMS should assign an auth token"
                await dev.send_status()

        run_async(run())

        # Device should appear in API — CMS sets name=device_id
        resp = api.get("/api/devices/reg-001")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "pending"
        assert data["name"] == "reg-001"

    def test_device_reconnects_with_auth_token(self, api, ws_url, e2e_server):
        """A device that reconnects with its saved auth token should succeed."""

        saved_token = None

        async def first_connect():
            nonlocal saved_token
            async with FakeDevice("reconnect-001", ws_url) as dev:
                saved_token = dev.auth_token
                await dev.send_status()

        run_async(first_connect())
        assert saved_token

        # Reconnect with the saved token
        async def reconnect():
            async with FakeDevice("reconnect-001", ws_url, auth_token=saved_token) as dev:
                await dev.send_status()
                return dev.sync_message

        sync = run_async(reconnect())
        assert sync is not None


class TestDeviceAdoption:
    """Admin adopts a pending device, device receives config."""

    def test_adopt_device_via_api(self, api, ws_url, e2e_server):
        """Adopting a device changes its status to 'adopted'."""

        async def register():
            async with FakeDevice("adopt-001", ws_url) as dev:
                await dev.send_status()

        run_async(register())

        resp = api.post("/api/devices/adopt-001/adopt")
        assert resp.status_code == 200

        resp = api.get("/api/devices/adopt-001")
        assert resp.json()["status"] == "adopted"

    def test_adopted_device_gets_api_key_on_connect(self, api, ws_url, e2e_server):
        """An adopted device should receive an API key on connection."""
        saved_token = None

        async def register():
            nonlocal saved_token
            async with FakeDevice("apikey-001", ws_url) as dev:
                saved_token = dev.auth_token
                await dev.send_status()

        run_async(register())
        api.post("/api/devices/apikey-001/adopt")

        async def reconnect():
            async with FakeDevice("apikey-001", ws_url, auth_token=saved_token) as dev:
                await dev.send_status()
                return dev.api_key

        key = run_async(reconnect())
        assert key, "Adopted device should receive an API key"


class TestScheduleSync:
    """Schedules created in the CMS are synced to devices."""

    def test_device_receives_schedule_in_sync(self, api, ws_url, e2e_server):
        """When a schedule exists for a device, it appears in the sync message."""
        saved_token = None

        async def register():
            nonlocal saved_token
            async with FakeDevice("sched-sync-001", ws_url) as dev:
                saved_token = dev.auth_token
                await dev.send_status()

        run_async(register())
        api.post("/api/devices/sched-sync-001/adopt")

        # Upload a test asset (fake content — probe will fail but asset still created)
        api.create_asset("sync-test.mp4")
        assets = api.get("/api/assets")
        if not assets.json():
            pytest.skip("Could not create test asset")
        asset_id = assets.json()[0]["id"]

        resp = api.post("/api/schedules", json={
            "name": "Device Sync Test",
            "device_id": "sched-sync-001",
            "asset_id": asset_id,
            "start_time": "00:00",
            "end_time": "23:59",
        })
        assert resp.status_code == 201

        async def reconnect():
            async with FakeDevice("sched-sync-001", ws_url, auth_token=saved_token) as dev:
                await dev.send_status()
                return dev.sync_message

        sync = run_async(reconnect())
        assert sync is not None
        schedules = sync.get("schedules", [])
        assert len(schedules) >= 1
        names = [s["name"] for s in schedules]
        assert "Device Sync Test" in names


class TestDeviceDashboard:
    """Device status is reflected in the web dashboard."""

    def test_connected_device_shows_online(self, page, ws_url, base_url, e2e_server):
        """A connected device should appear on the devices page."""
        device_ready = threading.Event()

        def run_device():
            async def _inner():
                async with FakeDevice("dash-001", ws_url) as dev:
                    await dev.send_status()
                    device_ready.set()
                    await asyncio.sleep(5)
            asyncio.run(_inner())

        thread = threading.Thread(target=run_device, daemon=True)
        thread.start()
        device_ready.wait(timeout=10)

        cookies = _login_cookies(base_url)
        with httpx.Client(base_url=base_url, cookies=cookies) as c:
            c.post("/api/devices/dash-001/adopt")

        page.goto("/devices")
        page.wait_for_load_state("domcontentloaded")

        # Device row uses data-device-id attribute
        device_row = page.locator('[data-device-id="dash-001"]').first
        device_row.wait_for(timeout=5000)

        thread.join(timeout=2)


class TestDeviceCommands:
    """CMS sends commands to connected devices."""

    def test_reboot_command_reaches_device(self, ws_url, base_url, e2e_server):
        """Sending a reboot command via API should deliver it to the device."""

        async def run():
            async with FakeDevice("reboot-001", ws_url) as dev:
                await dev.send_status()

                async with httpx.AsyncClient(base_url=base_url) as c:
                    r = await c.post(
                        "/login",
                        data={"username": "admin", "password": "testpass"},
                        follow_redirects=False,
                    )
                    cookies = dict(r.cookies)

                    await c.post("/api/devices/reboot-001/adopt", cookies=cookies)
                    await dev.send_status()
                    await asyncio.sleep(0.5)
                    await c.post("/api/devices/reboot-001/reboot", cookies=cookies)

                msg = await dev.wait_for_message("reboot", timeout=3.0)
                return msg

        msg = run_async(run())
        assert msg is not None, "Device should have received reboot command"
        assert msg["type"] == "reboot"
