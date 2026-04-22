"""Tests for device log collection feature."""

import asyncio
import json

import pytest
import pytest_asyncio

from cms.models.device import Device, DeviceStatus
from cms.services.device_manager import DeviceManager


# ── DeviceManager log request/response tests ──


class FakeWS:
    """Fake WebSocket that records sent messages."""

    def __init__(self):
        self.sent = []

    async def send_json(self, data: dict):
        self.sent.append(data)


class TestDeviceManagerLogRequests:
    @pytest.mark.asyncio
    async def test_request_logs_sends_message(self):
        dm = DeviceManager()
        ws = FakeWS()
        dm.register("dev-1", ws)

        # Start the request but don't await it — we need to resolve it
        task = asyncio.create_task(dm.request_logs("dev-1", since="1h", timeout=2.0))

        # Give the task a chance to send the message
        await asyncio.sleep(0.05)

        # A request_logs message should have been sent
        assert len(ws.sent) == 1
        msg = ws.sent[0]
        assert msg["type"] == "request_logs"
        assert msg["since"] == "1h"
        assert "request_id" in msg

        # Resolve with fake logs
        dm.resolve_log_request(msg["request_id"], {"agora-player": "some logs"})
        result = await task
        assert result == {"agora-player": "some logs"}

    @pytest.mark.asyncio
    async def test_request_logs_with_service_filter(self):
        dm = DeviceManager()
        ws = FakeWS()
        dm.register("dev-1", ws)

        task = asyncio.create_task(
            dm.request_logs("dev-1", services=["agora-api"], timeout=2.0)
        )
        await asyncio.sleep(0.05)

        msg = ws.sent[0]
        assert msg["services"] == ["agora-api"]

        dm.resolve_log_request(msg["request_id"], {"agora-api": "api logs"})
        result = await task
        assert result == {"agora-api": "api logs"}

    @pytest.mark.asyncio
    async def test_request_logs_timeout(self):
        dm = DeviceManager()
        ws = FakeWS()
        dm.register("dev-1", ws)

        with pytest.raises(TimeoutError):
            await dm.request_logs("dev-1", timeout=0.1)

    @pytest.mark.asyncio
    async def test_request_logs_device_not_connected(self):
        dm = DeviceManager()

        with pytest.raises(ValueError, match="not connected"):
            await dm.request_logs("nonexistent", timeout=1.0)

    @pytest.mark.asyncio
    async def test_resolve_log_request_with_error(self):
        dm = DeviceManager()
        ws = FakeWS()
        dm.register("dev-1", ws)

        task = asyncio.create_task(dm.request_logs("dev-1", timeout=2.0))
        await asyncio.sleep(0.05)

        msg = ws.sent[0]
        dm.resolve_log_request(msg["request_id"], {}, error="journalctl not available")

        with pytest.raises(RuntimeError, match="journalctl not available"):
            await task

    def test_resolve_unknown_request_id(self):
        dm = DeviceManager()
        # Should not raise
        dm.resolve_log_request("nonexistent-id", {"svc": "data"})


# ── REST API tests ──


@pytest_asyncio.fixture
async def device_in_db(db_session):
    """Create a test device in the database."""
    device = Device(
        id="log-test-device",
        name="Log Test Device",
        status=DeviceStatus.ADOPTED,
        firmware_version="1.0.0",
        storage_capacity_mb=1000,
        storage_used_mb=100,
    )
    db_session.add(device)
    await db_session.commit()
    return device


class TestDeviceLogsAPI:
    @pytest.mark.asyncio
    async def test_request_logs_device_not_found(self, client):
        resp = await client.post("/api/devices/nonexistent/logs")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_request_logs_device_offline(self, client, device_in_db):
        resp = await client.post(
            "/api/devices/log-test-device/logs",
            json={"since": "1h"},
        )
        assert resp.status_code == 409
        assert "not connected" in resp.json()["detail"]

    @pytest.mark.asyncio
    @pytest.mark.timeout(60)
    async def test_request_logs_success(self, client, device_in_db):
        from cms.services.device_manager import device_manager

        ws = FakeWS()
        device_manager.register("log-test-device", ws)

        async def fake_resolve():
            """Simulate the device responding with logs."""
            # Wait for the WS message to arrive (PostgreSQL DB queries
            # may take longer than SQLite, so a fixed sleep is racy).
            for _ in range(200):
                if ws.sent:
                    break
                await asyncio.sleep(0.05)
            msg = ws.sent[-1]
            device_manager.resolve_log_request(
                msg["request_id"],
                {"agora-player": "player log output", "agora-api": "api log output"},
            )

        try:
            task = asyncio.create_task(fake_resolve())
            resp = await client.post(
                "/api/devices/log-test-device/logs",
                json={"since": "1h"},
            )
            await task
            assert resp.status_code == 200
            data = resp.json()
            assert data["device_id"] == "log-test-device"
            assert "agora-player" in data["logs"]
            assert "agora-api" in data["logs"]
        finally:
            device_manager.disconnect("log-test-device")


# ── Log download (zip) endpoint tests ──


class TestLogDownloadAPI:
    @pytest.mark.asyncio
    async def test_download_cms_only(self, client):
        """Download zip with only CMS logs (no devices selected)."""
        resp = await client.post(
            "/api/logs/download",
            json={"device_ids": [], "include_cms": True, "since": "1h"},
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/zip"
        assert "agora-logs-" in resp.headers.get("content-disposition", "")

    @pytest.mark.asyncio
    async def test_download_offline_device(self, client, device_in_db):
        """Offline devices should get a not_connected.txt in the zip."""
        import io
        import zipfile

        resp = await client.post(
            "/api/logs/download",
            json={"device_ids": ["log-test-device"], "include_cms": False, "since": "1h"},
        )
        assert resp.status_code == 200

        zf = zipfile.ZipFile(io.BytesIO(resp.content))
        names = zf.namelist()
        assert any("not_connected" in n for n in names)

    @pytest.mark.asyncio
    async def test_download_nothing_selected(self, client):
        """No devices and no CMS logs should still return a valid (empty) zip."""
        resp = await client.post(
            "/api/logs/download",
            json={"device_ids": [], "include_cms": False, "since": "1h"},
        )
        assert resp.status_code == 200


# ── CMS-only log endpoint (new async UI flow) ──


class TestCmsLogsEndpoint:
    @pytest.mark.asyncio
    async def test_cms_logs_returns_zip(self, client):
        """GET /api/cms/logs returns a zip containing cms/cms.log."""
        import io
        import zipfile

        resp = await client.get("/api/cms/logs")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/zip"
        assert "agora-cms-logs-" in resp.headers.get("content-disposition", "")

        zf = zipfile.ZipFile(io.BytesIO(resp.content))
        assert "cms/cms.log" in zf.namelist()


# ── Protocol message tests ──


class TestProtocolMessages:
    def test_request_logs_message(self):
        from cms.schemas.protocol import RequestLogsMessage

        msg = RequestLogsMessage(
            request_id="test-123",
            services=["agora-player"],
            since="6h",
        )
        data = msg.model_dump(mode="json")
        assert data["type"] == "request_logs"
        assert data["request_id"] == "test-123"
        assert data["services"] == ["agora-player"]
        assert data["since"] == "6h"
        assert data["protocol_version"] == 2

    def test_logs_response_message(self):
        from cms.schemas.protocol import LogsResponseMessage

        msg = LogsResponseMessage(
            request_id="test-123",
            device_id="dev-1",
            logs={"agora-player": "some log output"},
        )
        data = msg.model_dump(mode="json")
        assert data["type"] == "logs_response"
        assert data["logs"]["agora-player"] == "some log output"
        assert data["error"] is None

    def test_request_logs_defaults(self):
        from cms.schemas.protocol import RequestLogsMessage

        msg = RequestLogsMessage(request_id="test-456")
        assert msg.services is None
        assert msg.since == "24h"
