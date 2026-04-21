"""Unit tests for the WPS upstream-webhook receiver.

These tests wire up only the webhook router (not the full CMS app) with
``get_db`` overridden to yield a fake async session, so they run
without Postgres and without the rest of the lifespan machinery.  The
``Settings`` lookup is monkeypatched to a hand-built instance so tests
can control the WPS connection string (and therefore the signing key).
"""

from __future__ import annotations

import hashlib
import hmac
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


WPS_KEY = "test-webhook-key"
CONN_STR = f"Endpoint=http://broker:7080;AccessKey={WPS_KEY};Version=1.0;"


def _sig(connection_id: str, key: str = WPS_KEY) -> str:
    digest = hmac.new(key.encode(), connection_id.encode(), hashlib.sha256).hexdigest()
    return f"sha256={digest}"


class _FakeSession:
    """Minimal ``AsyncSession`` stand-in — webhook tests don't need a real DB.

    ``execute`` returns a result whose ``.scalar_one_or_none()`` is
    driven by ``device_row`` (set per test).
    """

    def __init__(self, device_row=None):
        self.device_row = device_row
        self.commits = 0
        self.added: list = []

    async def execute(self, _stmt):
        row = self.device_row
        result = MagicMock()
        result.scalar_one_or_none = MagicMock(return_value=row)
        return result

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1

    async def refresh(self, obj, *args, **kwargs):  # pragma: no cover - not hit here
        pass


@pytest_asyncio.fixture
async def app_and_session(monkeypatch):
    """Build a fresh FastAPI app containing only the wps_webhook router."""
    # Fresh DeviceManager for every test — isolation across webhook events.
    from cms.services import device_manager as dm_module
    dm_module.device_manager._connections.clear()
    dm_module.device_manager._pending_log_requests.clear()

    # Stub Settings that satisfies the receiver's needs.
    class _S:
        wps_connection_string = CONN_STR
        wps_hub = "agora"
        wps_webhook_allowed_origin = None
        asset_base_url = None

    from cms.routers import wps_webhook as wh

    monkeypatch.setattr(wh, "get_settings", lambda: _S())

    session = _FakeSession()

    async def _fake_db():
        yield session

    from cms.database import get_db

    app = FastAPI()
    app.include_router(wh.router)
    app.dependency_overrides[get_db] = _fake_db

    yield app, session


@pytest_asyncio.fixture
async def client(app_and_session):
    app, _ = app_and_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


# ---------------------------------------------------------------- OPTIONS


@pytest.mark.asyncio
class TestAbuseProtectionHandshake:
    async def test_echoes_request_origin(self, client):
        r = await client.options(
            "/internal/wps/events",
            headers={"WebHook-Request-Origin": "foo.webpubsub.azure.com"},
        )
        assert r.status_code == 200
        assert r.headers["WebHook-Allowed-Origin"] == "foo.webpubsub.azure.com"

    async def test_wildcard_when_origin_unset(self, client):
        r = await client.options("/internal/wps/events")
        assert r.status_code == 200
        assert r.headers["WebHook-Allowed-Origin"] == "*"

    async def test_configured_origin_wins(self, monkeypatch, app_and_session, client):
        from cms.routers import wps_webhook as wh

        class _S:
            wps_connection_string = CONN_STR
            wps_hub = "agora"
            wps_webhook_allowed_origin = "only.me"
            asset_base_url = None

        monkeypatch.setattr(wh, "get_settings", lambda: _S())
        r = await client.options(
            "/internal/wps/events",
            headers={"WebHook-Request-Origin": "other.example"},
        )
        assert r.status_code == 200
        assert r.headers["WebHook-Allowed-Origin"] == "only.me"


# ---------------------------------------------------------------- signature


@pytest.mark.asyncio
class TestSignatureVerification:
    async def test_rejects_bad_signature(self, client):
        r = await client.post(
            "/internal/wps/events",
            content=b"{}",
            headers={
                "content-type": "application/json",
                "ce-type": "azure.webpubsub.sys.connected",
                "ce-connectionId": "conn-1",
                "ce-userId": "pi-1",
                "ce-signature": "sha256=deadbeef",
            },
        )
        assert r.status_code == 401

    async def test_rejects_missing_signature(self, client):
        r = await client.post(
            "/internal/wps/events",
            content=b"{}",
            headers={
                "content-type": "application/json",
                "ce-type": "azure.webpubsub.sys.connected",
                "ce-connectionId": "conn-1",
                "ce-userId": "pi-1",
            },
        )
        assert r.status_code == 401

    async def test_missing_connection_id_rejected(self, client):
        r = await client.post(
            "/internal/wps/events",
            content=b"{}",
            headers={
                "content-type": "application/json",
                "ce-type": "azure.webpubsub.sys.connected",
                "ce-userId": "pi-1",
                "ce-signature": _sig("whatever"),
            },
        )
        assert r.status_code == 400


# ---------------------------------------------------------------- system events


@pytest.mark.asyncio
class TestSystemEvents:
    async def test_connected_registers_remote(self, client):
        from cms.services.device_manager import device_manager

        cid = "conn-register"
        r = await client.post(
            "/internal/wps/events",
            content=b"{}",
            headers={
                "content-type": "application/json",
                "ce-type": "azure.webpubsub.sys.connected",
                "ce-connectionId": cid,
                "ce-userId": "pi-register",
                "ce-eventName": "connected",
                "ce-signature": _sig(cid),
            },
        )
        assert r.status_code == 204
        assert device_manager.is_connected("pi-register")
        conn = device_manager.get("pi-register")
        assert conn is not None
        assert conn.websocket is None
        assert conn.connection_id == cid

    async def test_disconnected_removes_from_manager(self, client):
        from cms.services.device_manager import device_manager

        # Arrange: pre-register.
        device_manager.register_remote("pi-gone", connection_id="old-cid")
        assert device_manager.is_connected("pi-gone")

        cid = "old-cid"
        r = await client.post(
            "/internal/wps/events",
            content=b"{}",
            headers={
                "content-type": "application/json",
                "ce-type": "azure.webpubsub.sys.disconnected",
                "ce-connectionId": cid,
                "ce-userId": "pi-gone",
                "ce-eventName": "disconnected",
                "ce-signature": _sig(cid),
            },
        )
        assert r.status_code == 204
        assert not device_manager.is_connected("pi-gone")


# ---------------------------------------------------------------- user events


@pytest.mark.asyncio
class TestUserEvents:
    async def test_unknown_device_is_logged_and_204(self, client, app_and_session):
        """Until the register-over-WPS handshake is ported, messages
        from devices with no DB row are dropped with a warning."""
        app, session = app_and_session
        session.device_row = None

        cid = "conn-unknown"
        r = await client.post(
            "/internal/wps/events",
            content=json.dumps({"type": "STATUS"}).encode(),
            headers={
                "content-type": "application/json",
                "ce-type": "azure.webpubsub.user.message",
                "ce-connectionId": cid,
                "ce-userId": "pi-unknown",
                "ce-eventName": "message",
                "ce-signature": _sig(cid),
            },
        )
        assert r.status_code == 204

    async def test_known_device_dispatches(self, client, app_and_session):
        """Valid CE on a user event routes to dispatch_device_message."""
        app, session = app_and_session
        # Fake device row with just enough attrs for InboundContext build.
        device = MagicMock()
        device.id = "pi-known"
        device.name = "Lobby"
        device.group_id = None
        device.status = MagicMock(value="adopted")
        session.device_row = device

        cid = "conn-known"
        payload = {"type": "STATUS", "mode": "play"}

        with patch(
            "cms.routers.wps_webhook.dispatch_device_message",
            new=AsyncMock(return_value=None),
        ) as mock_dispatch:
            r = await client.post(
                "/internal/wps/events",
                content=json.dumps(payload).encode(),
                headers={
                    "content-type": "application/json",
                    "ce-type": "azure.webpubsub.user.message",
                    "ce-connectionId": cid,
                    "ce-userId": "pi-known",
                    "ce-eventName": "message",
                    "ce-signature": _sig(cid),
                },
            )
            assert r.status_code == 204
            mock_dispatch.assert_awaited_once()
            _, kwargs = mock_dispatch.call_args
            assert kwargs["msg"] == payload
            assert kwargs["ctx"].device_id == "pi-known"
            assert kwargs["ctx"].device is device
            # send closure should be wired to the current transport.
            assert callable(kwargs["send"])

    async def test_non_json_body_is_400(self, client, app_and_session):
        _, session = app_and_session
        session.device_row = MagicMock(
            id="pi-known", name="x", group_id=None, status=MagicMock(value="adopted"),
        )
        cid = "conn-badbody"
        r = await client.post(
            "/internal/wps/events",
            content=b"not json at all",
            headers={
                "content-type": "application/json",
                "ce-type": "azure.webpubsub.user.message",
                "ce-connectionId": cid,
                "ce-userId": "pi-known",
                "ce-eventName": "message",
                "ce-signature": _sig(cid),
            },
        )
        assert r.status_code == 400


# ---------------------------------------------------------------- unknown type


@pytest.mark.asyncio
class TestUnknownEventType:
    async def test_400_on_unknown(self, client):
        cid = "conn-x"
        r = await client.post(
            "/internal/wps/events",
            content=b"{}",
            headers={
                "content-type": "application/json",
                "ce-type": "azure.webpubsub.sys.somethingnew",
                "ce-connectionId": cid,
                "ce-userId": "pi-1",
                "ce-signature": _sig(cid),
            },
        )
        assert r.status_code == 400


# ---------------------------------------------------------------- multi-key


@pytest.mark.asyncio
class TestMultiKeyAcceptance:
    async def test_any_configured_key_verifies(self, monkeypatch, app_and_session, client):
        """Connection strings may carry primary+secondary AccessKey for rotation."""
        from cms.routers import wps_webhook as wh

        conn = f"Endpoint=http://b;AccessKey=new-key;AccessKey=old-key;Version=1.0;"

        class _S:
            wps_connection_string = conn
            wps_hub = "agora"
            wps_webhook_allowed_origin = None
            asset_base_url = None

        monkeypatch.setattr(wh, "get_settings", lambda: _S())

        cid = "conn-rot"
        # Sign with the OLD key — receiver should still accept.
        r = await client.post(
            "/internal/wps/events",
            content=b"{}",
            headers={
                "content-type": "application/json",
                "ce-type": "azure.webpubsub.sys.connected",
                "ce-connectionId": cid,
                "ce-userId": "pi-rot",
                "ce-signature": _sig(cid, key="old-key"),
            },
        )
        assert r.status_code == 204
