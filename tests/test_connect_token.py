"""Tests for ``POST /api/devices/{id}/connect-token``.

The endpoint is device-originated (authenticates via
``X-Device-API-Key``) and returns a WPS client access token when
``DEVICE_TRANSPORT=wps``.  Tests mount only the sub-router so they
run without Postgres; the DB session and settings are faked.
"""

from __future__ import annotations

import hashlib
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


class _FakeSession:
    def __init__(self, device=None):
        self.device = device

    async def execute(self, _stmt):
        result = MagicMock()
        result.scalar_one_or_none = MagicMock(return_value=self.device)
        return result


def _fake_device(device_id: str, api_key: str):
    d = MagicMock()
    d.id = device_id
    d.device_api_key_hash = _hash(api_key)
    d.previous_api_key_hash = None
    d.api_key_rotated_at = None
    return d


def _install_settings(monkeypatch, *, transport_mode: str = "wps"):
    class _S:
        device_transport = transport_mode
        wps_connection_string = "Endpoint=http://broker:7080;AccessKey=k;Version=1.0;"
        wps_hub = "agora"
        wps_token_lifetime_minutes = 30

    from cms.routers import devices as devices_module
    monkeypatch.setattr(devices_module, "get_settings", lambda: _S())


@pytest_asyncio.fixture
async def app_and_session(monkeypatch):
    """Build a minimal FastAPI app with only the device-originated router."""
    from cms.routers.devices import device_originated_router
    from cms.database import get_db

    session = _FakeSession()

    async def _fake_db():
        yield session

    app = FastAPI()
    app.include_router(device_originated_router)
    app.dependency_overrides[get_db] = _fake_db

    yield app, session


@pytest_asyncio.fixture
async def client(app_and_session):
    app, _ = app_and_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


@pytest.mark.asyncio
class TestConnectToken:
    async def test_returns_url_and_token(self, monkeypatch, app_and_session, client):
        _install_settings(monkeypatch, transport_mode="wps")

        _, session = app_and_session
        api_key = "dev-key-123"
        session.device = _fake_device("pi-1", api_key)

        fake_transport = MagicMock()
        fake_transport.get_client_access_token = AsyncMock(
            return_value={
                "url": "wss://broker/client/hubs/agora?access_token=jwtjwt",
                "token": "jwtjwt",
            }
        )
        from cms.routers import devices as devices_module
        monkeypatch.setattr(devices_module, "get_transport", lambda: fake_transport)

        r = await client.post(
            "/api/devices/pi-1/connect-token",
            headers={"X-Device-API-Key": api_key},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["url"].startswith("wss://broker/")
        assert body["token"] == "jwtjwt"

        fake_transport.get_client_access_token.assert_awaited_once_with(
            "pi-1", minutes_to_expire=30,
        )

    async def test_404_when_transport_is_local(self, monkeypatch, app_and_session, client):
        _install_settings(monkeypatch, transport_mode="local")
        _, session = app_and_session
        session.device = _fake_device("pi-1", "any-key")

        r = await client.post(
            "/api/devices/pi-1/connect-token",
            headers={"X-Device-API-Key": "any-key"},
        )
        assert r.status_code == 404

    async def test_401_without_api_key(self, monkeypatch, client):
        _install_settings(monkeypatch, transport_mode="wps")
        r = await client.post("/api/devices/pi-1/connect-token")
        assert r.status_code == 401

    async def test_401_with_wrong_api_key(self, monkeypatch, app_and_session, client):
        _install_settings(monkeypatch, transport_mode="wps")
        _, session = app_and_session
        session.device = _fake_device("pi-1", "correct-key")

        r = await client.post(
            "/api/devices/pi-1/connect-token",
            headers={"X-Device-API-Key": "wrong-key"},
        )
        assert r.status_code == 401

    async def test_404_when_device_missing(self, monkeypatch, app_and_session, client):
        _install_settings(monkeypatch, transport_mode="wps")
        _, session = app_and_session
        session.device = None  # device lookup returns nothing

        r = await client.post(
            "/api/devices/pi-ghost/connect-token",
            headers={"X-Device-API-Key": "any"},
        )
        assert r.status_code == 404
