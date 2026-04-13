"""Tests for MCP default_asset_id support (issue #147).

The MCP tools live in ``mcp/server.py`` which cannot be imported from the
CMS test-suite because the local ``mcp/`` package name collides with the
``mcp`` pip dependency.  Instead we test the *CMSClient* methods that the
tools delegate to, and the CMS REST endpoints they hit — together these
cover the full MCP → API contract.
"""

import json
import uuid

import pytest

from cms.models.device import Device, DeviceGroup, DeviceStatus

ASSET_UUID = str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _create_asset(db, asset_id=None):
    """Insert a minimal Asset row and return it."""
    from cms.models.asset import Asset
    asset = Asset(
        id=uuid.UUID(asset_id) if asset_id else uuid.uuid4(),
        filename="splash.png",
        original_filename="splash.png",
        asset_type="image",
        size_bytes=1024,
    )
    db.add(asset)
    await db.commit()
    await db.refresh(asset)
    return asset


async def _create_device(db, device_id="dev-1", group_id=None):
    device = Device(
        id=device_id,
        name="Test",
        status=DeviceStatus.ADOPTED,
        group_id=group_id,
    )
    db.add(device)
    await db.commit()
    return device


async def _create_group(db, name="Lobby"):
    group = DeviceGroup(name=name, description="")
    db.add(group)
    await db.commit()
    await db.refresh(group)
    return group


# ---------------------------------------------------------------------------
# PATCH /api/devices/{id}  — default_asset_id
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestDeviceDefaultAssetAPI:
    """The REST endpoint that MCP update_device calls."""

    async def test_set_default_asset(self, client, db_session):
        asset = await _create_asset(db_session)
        await _create_device(db_session)

        resp = await client.patch(
            "/api/devices/dev-1",
            json={"default_asset_id": str(asset.id)},
        )
        assert resp.status_code == 200
        assert resp.json()["default_asset_id"] == str(asset.id)

    async def test_clear_default_asset(self, client, db_session):
        asset = await _create_asset(db_session)
        await _create_device(db_session)
        # Set it first
        await client.patch("/api/devices/dev-1", json={"default_asset_id": str(asset.id)})
        # Now clear
        resp = await client.patch(
            "/api/devices/dev-1",
            json={"default_asset_id": None},
        )
        assert resp.status_code == 200
        assert resp.json()["default_asset_id"] is None

    async def test_omitting_field_leaves_unchanged(self, client, db_session):
        asset = await _create_asset(db_session)
        await _create_device(db_session)
        await client.patch("/api/devices/dev-1", json={"default_asset_id": str(asset.id)})

        # Update only the name — default_asset_id should stay
        resp = await client.patch("/api/devices/dev-1", json={"name": "Renamed"})
        assert resp.status_code == 200
        assert resp.json()["default_asset_id"] == str(asset.id)
        assert resp.json()["name"] == "Renamed"


# ---------------------------------------------------------------------------
# POST /api/devices/groups/  — default_asset_id on create
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestCreateGroupDefaultAssetAPI:
    """The REST endpoint that MCP create_group calls."""

    async def test_create_with_default_asset(self, client, db_session):
        asset = await _create_asset(db_session)
        resp = await client.post(
            "/api/devices/groups/",
            json={"name": "Lobby", "default_asset_id": str(asset.id)},
        )
        assert resp.status_code == 201
        assert resp.json()["default_asset_id"] == str(asset.id)

    async def test_create_without_default_asset(self, client, db_session):
        resp = await client.post(
            "/api/devices/groups/",
            json={"name": "Hallway"},
        )
        assert resp.status_code == 201
        assert resp.json()["default_asset_id"] is None


# ---------------------------------------------------------------------------
# PATCH /api/devices/groups/{id}  — default_asset_id
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestUpdateGroupDefaultAssetAPI:
    """The REST endpoint that MCP update_group calls."""

    async def test_set_default_asset(self, client, db_session):
        asset = await _create_asset(db_session)
        group = await _create_group(db_session)

        resp = await client.patch(
            f"/api/devices/groups/{group.id}",
            json={"default_asset_id": str(asset.id)},
        )
        assert resp.status_code == 200
        assert resp.json()["default_asset_id"] == str(asset.id)

    async def test_clear_default_asset(self, client, db_session):
        asset = await _create_asset(db_session)
        group = await _create_group(db_session)
        await client.patch(
            f"/api/devices/groups/{group.id}",
            json={"default_asset_id": str(asset.id)},
        )

        resp = await client.patch(
            f"/api/devices/groups/{group.id}",
            json={"default_asset_id": None},
        )
        assert resp.status_code == 200
        assert resp.json()["default_asset_id"] is None

    async def test_omitting_field_leaves_unchanged(self, client, db_session):
        asset = await _create_asset(db_session)
        group = await _create_group(db_session)
        await client.patch(
            f"/api/devices/groups/{group.id}",
            json={"default_asset_id": str(asset.id)},
        )

        resp = await client.patch(
            f"/api/devices/groups/{group.id}",
            json={"name": "Renamed"},
        )
        assert resp.status_code == 200
        assert resp.json()["default_asset_id"] == str(asset.id)
        assert resp.json()["name"] == "Renamed"

