"""Tests for WebSocket device connection handler."""

import hashlib
import json
import time

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient


@pytest.mark.asyncio
class TestWebSocket:
    async def test_register_new_device(self, app):
        from starlette.testclient import TestClient

        with TestClient(app) as tc:
            with tc.websocket_connect("/ws/device") as ws:
                ws.send_json({
                    "type": "register",
                    "protocol_version": 1,
                    "device_id": "ws-test-001",
                    "auth_token": "",
                    "firmware_version": "1.0.0",
                    "storage_capacity_mb": 500,
                    "storage_used_mb": 100,
                })

                # Should receive auth_assigned
                msg = ws.receive_json()
                assert msg["type"] == "auth_assigned"
                assert "device_auth_token" in msg

                # New device is PENDING — no sync sent until adopted
                time.sleep(0.5)  # Allow server to finish before disconnect

                ws.close()

    async def test_register_known_device_valid_token(self, app, db_session):
        from cms.models.device import Device, DeviceStatus

        token = "test-token-12345"
        token_hash = hashlib.sha256(token.encode()).hexdigest()

        device = Device(
            id="ws-test-002",
            name="ws-test-002",
            status=DeviceStatus.ADOPTED,
            device_auth_token_hash=token_hash,
        )
        db_session.add(device)
        await db_session.commit()
        await db_session.close()

        from starlette.testclient import TestClient
        with TestClient(app) as tc:
            with tc.websocket_connect("/ws/device") as ws:
                ws.send_json({
                    "type": "register",
                    "protocol_version": 1,
                    "device_id": "ws-test-002",
                    "auth_token": token,
                    "firmware_version": "1.0.0",
                    "storage_capacity_mb": 500,
                })

                # Should receive sync (no auth_assigned since token already set)
                msg = ws.receive_json()
                assert msg["type"] == "sync"

                # Adopted devices also receive a config message (API key push)
                msg = ws.receive_json()
                assert msg["type"] == "config"

                time.sleep(0.5)  # Allow server to finish before disconnect

                ws.close()

    async def test_register_known_device_wrong_token(self, app, db_session):
        from cms.models.device import Device, DeviceStatus

        token_hash = hashlib.sha256(b"correct-token").hexdigest()

        device = Device(
            id="ws-test-003",
            name="ws-test-003",
            status=DeviceStatus.ADOPTED,
            device_auth_token_hash=token_hash,
        )
        db_session.add(device)
        await db_session.commit()
        await db_session.close()

        from starlette.testclient import TestClient
        with TestClient(app) as tc:
            with tc.websocket_connect("/ws/device") as ws:
                ws.send_json({
                    "type": "register",
                    "protocol_version": 1,
                    "device_id": "ws-test-003",
                    "auth_token": "wrong-token",
                })
                # Connection should close with error
                msg = ws.receive_json()
                assert "error" in msg

    async def test_wrong_protocol_version(self, app):
        from starlette.testclient import TestClient

        with TestClient(app) as tc:
            with tc.websocket_connect("/ws/device") as ws:
                ws.send_json({
                    "type": "register",
                    "protocol_version": 999,
                    "device_id": "ws-test-004",
                })
                msg = ws.receive_json()
                assert "error" in msg

    async def test_missing_device_id(self, app):
        from starlette.testclient import TestClient

        with TestClient(app) as tc:
            with tc.websocket_connect("/ws/device") as ws:
                ws.send_json({
                    "type": "register",
                    "protocol_version": 1,
                })
                msg = ws.receive_json()
                assert "error" in msg

    async def test_non_register_first_message(self, app):
        from starlette.testclient import TestClient

        with TestClient(app) as tc:
            with tc.websocket_connect("/ws/device") as ws:
                ws.send_json({"type": "status", "device_id": "x"})
                msg = ws.receive_json()
                assert "error" in msg

    async def test_status_message_updates_device(self, app):
        from starlette.testclient import TestClient

        with TestClient(app) as tc:
            with tc.websocket_connect("/ws/device") as ws:
                ws.send_json({
                    "type": "register",
                    "protocol_version": 1,
                    "device_id": "ws-test-status",
                    "auth_token": "",
                    "firmware_version": "1.0.0",
                    "storage_capacity_mb": 500,
                    "storage_used_mb": 50,
                })

                # Consume auth_assigned (no sync for unadopted device)
                ws.receive_json()

                # Send status
                ws.send_json({
                    "type": "status",
                    "device_id": "ws-test-status",
                    "mode": "splash",
                    "storage_used_mb": 200,
                })

                time.sleep(0.5)  # Allow server to process status before disconnect

                ws.close()

    async def test_reflashed_device_empty_token_gets_readopted(self, app, db_session):
        """A known device with a token hash that connects with empty token
        (simulating a re-flash / factory reset) should be reset to PENDING
        and assigned a new auth token, not rejected as orphaned."""
        from cms.models.device import Device, DeviceStatus

        token_hash = hashlib.sha256(b"old-token-from-before-reflash").hexdigest()

        device = Device(
            id="ws-test-reflash",
            name="ws-test-reflash",
            status=DeviceStatus.ADOPTED,
            device_auth_token_hash=token_hash,
        )
        db_session.add(device)
        await db_session.commit()
        await db_session.close()

        from starlette.testclient import TestClient
        with TestClient(app) as tc:
            with tc.websocket_connect("/ws/device") as ws:
                ws.send_json({
                    "type": "register",
                    "protocol_version": 1,
                    "device_id": "ws-test-reflash",
                    "auth_token": "",
                    "firmware_version": "2.0.0",
                    "storage_capacity_mb": 500,
                })

                # Should receive auth_assigned (new token), NOT an error
                msg = ws.receive_json()
                assert "error" not in msg, f"Got error instead of auth_assigned: {msg}"
                assert msg["type"] == "auth_assigned"
                assert "device_auth_token" in msg

                # Reflashed device is reset to PENDING — no sync until adopted
                time.sleep(0.5)  # Allow server to finish before disconnect

                ws.close()

    async def test_known_device_wrong_nonmempty_token_rejected(self, app, db_session):
        """A known device that sends a WRONG (non-empty) token should still
        be rejected as orphaned — this is a security measure."""
        from cms.models.device import Device, DeviceStatus

        token_hash = hashlib.sha256(b"correct-token").hexdigest()

        device = Device(
            id="ws-test-wrong-token",
            name="ws-test-wrong-token",
            status=DeviceStatus.ADOPTED,
            device_auth_token_hash=token_hash,
        )
        db_session.add(device)
        await db_session.commit()
        await db_session.close()

        from starlette.testclient import TestClient
        with TestClient(app) as tc:
            with tc.websocket_connect("/ws/device") as ws:
                ws.send_json({
                    "type": "register",
                    "protocol_version": 1,
                    "device_id": "ws-test-wrong-token",
                    "auth_token": "wrong-token-not-empty",
                })
                # Should be rejected
                msg = ws.receive_json()
                assert "error" in msg

    async def test_download_url_uses_host_header(self, app, db_session):
        """fetch_asset download_url should use the Host header the device
        connected with, not the server-side socket address (fixes #138)."""
        from cms.models.asset import Asset, AssetType
        from cms.models.device import Device, DeviceStatus

        token = "dl-url-token"
        token_hash = hashlib.sha256(token.encode()).hexdigest()

        asset = Asset(
            filename="test.mp4",
            asset_type=AssetType.VIDEO,
            size_bytes=1000,
            checksum="abc123",
        )
        db_session.add(asset)
        await db_session.flush()

        device = Device(
            id="ws-dl-url-001",
            name="dl-url-device",
            status=DeviceStatus.ADOPTED,
            device_auth_token_hash=token_hash,
            default_asset_id=asset.id,
        )
        db_session.add(device)
        await db_session.commit()
        await db_session.close()

        from starlette.testclient import TestClient
        with TestClient(app) as tc:
            with tc.websocket_connect(
                "/ws/device",
                headers={"host": "192.168.1.198:8080"},
            ) as ws:
                ws.send_json({
                    "type": "register",
                    "protocol_version": 1,
                    "device_id": "ws-dl-url-001",
                    "auth_token": token,
                    "firmware_version": "1.0.0",
                    "storage_capacity_mb": 500,
                })

                # Consume sync
                msg = ws.receive_json()
                assert msg["type"] == "sync"

                # Should receive fetch_asset for default asset
                msg = ws.receive_json()
                assert msg["type"] == "fetch_asset"
                assert msg["download_url"].startswith("http://192.168.1.198:8080/")
                assert "/api/assets/" in msg["download_url"]

                # Consume config (API key push)
                msg = ws.receive_json()
                assert msg["type"] == "config"

                time.sleep(0.5)
                ws.close()

    async def test_download_url_uses_config_override(self, app, db_session):
        """When AGORA_CMS_ASSET_BASE_URL is set, fetch_asset download_url
        should use that value instead of the Host header."""
        from cms.auth import get_settings
        from cms.models.asset import Asset, AssetType
        from cms.models.device import Device, DeviceStatus

        token = "dl-cfg-token"
        token_hash = hashlib.sha256(token.encode()).hexdigest()

        asset = Asset(
            filename="test-cfg.mp4",
            asset_type=AssetType.VIDEO,
            size_bytes=2000,
            checksum="def456",
        )
        db_session.add(asset)
        await db_session.flush()

        device = Device(
            id="ws-dl-cfg-001",
            name="dl-cfg-device",
            status=DeviceStatus.ADOPTED,
            device_auth_token_hash=token_hash,
            default_asset_id=asset.id,
        )
        db_session.add(device)
        await db_session.commit()
        await db_session.close()

        settings = get_settings()
        original_value = settings.asset_base_url
        settings.asset_base_url = "https://cdn.example.com"

        try:
            from starlette.testclient import TestClient
            with TestClient(app) as tc:
                with tc.websocket_connect("/ws/device") as ws:
                    ws.send_json({
                        "type": "register",
                        "protocol_version": 1,
                        "device_id": "ws-dl-cfg-001",
                        "auth_token": token,
                        "firmware_version": "1.0.0",
                        "storage_capacity_mb": 500,
                    })

                    # Consume sync
                    msg = ws.receive_json()
                    assert msg["type"] == "sync"

                    # Should receive fetch_asset with CDN URL
                    msg = ws.receive_json()
                    assert msg["type"] == "fetch_asset"
                    assert msg["download_url"].startswith("https://cdn.example.com/")

                    # Consume config (API key push)
                    msg = ws.receive_json()
                    assert msg["type"] == "config"

                    time.sleep(0.5)
                    ws.close()
        finally:
            settings.asset_base_url = original_value
