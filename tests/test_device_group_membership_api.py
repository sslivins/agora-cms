"""Stage 6 device-group membership API tests (#863)."""

from __future__ import annotations

import uuid
from datetime import datetime, time, timezone
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from cms.auth import hash_password
from cms.models.asset import Asset
from cms.models.audit_log import AuditLog
from cms.models.device import Device, DeviceGroup, DeviceStatus
from cms.models.device_group_membership import DeviceGroupMembership
from cms.models.schedule import Schedule
from cms.models.user import Role, User, UserGroup
from cms.permissions import (
    BUILTIN_ROLES,
    DEVICES_READ,
    DEVICES_WRITE,
)


async def _get_role_id(db, name: str) -> uuid.UUID:
    result = await db.execute(select(Role).where(Role.name == name))
    role = result.scalar_one_or_none()
    if role is None:
        template = BUILTIN_ROLES[name]
        role = Role(
            name=name,
            description=template.get("description", ""),
            permissions=template["permissions"],
        )
        db.add(role)
        await db.flush()
    return role.id


async def _create_user(
    db,
    *,
    email: str,
    role_name: str = "Operator",
    role_id: uuid.UUID | None = None,
    group_ids: list[uuid.UUID] | None = None,
) -> User:
    if role_id is None:
        role_id = await _get_role_id(db, role_name)
    username = email.split("@")[0]
    user = User(
        username=username,
        email=email,
        display_name=username,
        password_hash=hash_password("password123"),
        role_id=role_id,
        is_active=True,
        must_change_password=False,
    )
    db.add(user)
    await db.flush()
    for group_id in group_ids or []:
        db.add(UserGroup(user_id=user.id, group_id=group_id))
    await db.commit()
    await db.refresh(user, ["role"])
    return user


async def _login_as(app, email: str) -> AsyncClient:
    transport = ASGITransport(app=app)
    client = AsyncClient(transport=transport, base_url="http://test")
    await client.post(
        "/login",
        data={"username": email.split("@")[0], "password": "password123"},
        follow_redirects=False,
    )
    return client


async def _create_group(db, name: str) -> DeviceGroup:
    group = DeviceGroup(name=name, description="")
    db.add(group)
    await db.commit()
    await db.refresh(group)
    return group


async def _create_device(
    db,
    *,
    device_id: str,
    name: str = "Test Device",
    group_id: uuid.UUID | None = None,
) -> Device:
    device = Device(
        id=device_id,
        name=name,
        status=DeviceStatus.ADOPTED,
        group_id=group_id,
    )
    db.add(device)
    await db.flush()
    if group_id is not None:
        db.add(DeviceGroupMembership(device_id=device.id, group_id=group_id))
    await db.commit()
    await db.refresh(device)
    return device


async def _create_schedule(db, *, group_id: uuid.UUID, name: str) -> Schedule:
    asset = Asset(
        id=uuid.uuid4(),
        filename=f"{name}.png",
        original_filename=f"{name}.png",
        asset_type="image",
        size_bytes=1024,
    )
    db.add(asset)
    await db.flush()
    schedule = Schedule(
        name=name,
        group_id=group_id,
        asset_id=asset.id,
        start_time=time(9, 0),
        end_time=time(17, 0),
        start_date=datetime.now(timezone.utc),
        end_date=None,
        priority=0,
        enabled=True,
    )
    db.add(schedule)
    await db.commit()
    await db.refresh(schedule)
    return schedule


async def _device_memberships(db, device_id: str) -> set[uuid.UUID]:
    rows = await db.execute(
        select(DeviceGroupMembership.group_id).where(
            DeviceGroupMembership.device_id == device_id
        )
    )
    return set(rows.scalars().all())


@pytest.mark.asyncio
class TestDeviceGroupMembershipEndpoints:
    async def test_add_membership_happy_path_syncs_and_audits(self, client, db_session):
        group_a = await _create_group(db_session, "Alpha")
        group_b = await _create_group(db_session, "Beta")
        await _create_schedule(db_session, group_id=group_b.id, name="Beta Schedule")
        device = await _create_device(
            db_session,
            device_id="g2m-add-001",
            group_id=group_a.id,
        )

        with patch("cms.routers.devices.push_sync_to_device", new_callable=AsyncMock) as mock_sync:
            resp = await client.post(
                f"/api/devices/{device.id}/groups",
                json={"group_id": str(group_b.id)},
            )

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["changed"] is True
        assert data["dry_run"] is False
        assert data["group_id"] == str(group_a.id)
        assert set(data["group_ids"]) == {str(group_a.id), str(group_b.id)}
        assert {group["id"] for group in data["groups"]} == {
            str(group_a.id),
            str(group_b.id),
        }
        assert [item["name"] for item in data["schedules_added"]] == ["Beta Schedule"]
        assert data["schedules_removed"] == []
        mock_sync.assert_called_once()
        assert mock_sync.call_args[0][0] == device.id
        assert await _device_memberships(db_session, device.id) == {group_a.id, group_b.id}

        audit = await db_session.execute(
            select(AuditLog).where(AuditLog.action == "device.group.add")
        )
        assert audit.scalar_one_or_none() is not None

    async def test_add_membership_is_idempotent(self, client, db_session):
        group_a = await _create_group(db_session, "Alpha")
        group_b = await _create_group(db_session, "Beta")
        device = await _create_device(
            db_session,
            device_id="g2m-add-002",
            group_id=group_a.id,
        )
        db_session.add(DeviceGroupMembership(device_id=device.id, group_id=group_b.id))
        await db_session.commit()

        with patch("cms.routers.devices.push_sync_to_device", new_callable=AsyncMock) as mock_sync:
            resp = await client.post(
                f"/api/devices/{device.id}/groups",
                json={"group_id": str(group_b.id)},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["changed"] is False
        assert set(data["group_ids"]) == {str(group_a.id), str(group_b.id)}
        mock_sync.assert_not_called()

    async def test_remove_membership_allows_last_group_removal(self, client, db_session):
        group_a = await _create_group(db_session, "Alpha")
        await _create_schedule(db_session, group_id=group_a.id, name="Alpha Schedule")
        device = await _create_device(
            db_session,
            device_id="g2m-remove-001",
            group_id=group_a.id,
        )
        device_id = device.id

        with patch("cms.routers.devices.push_sync_to_device", new_callable=AsyncMock) as mock_sync:
            resp = await client.delete(
                f"/api/devices/{device.id}/groups/{group_a.id}",
            )

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["changed"] is True
        assert data["group_id"] is None
        assert data["group_ids"] == []
        assert data["schedules_added"] == []
        assert [item["name"] for item in data["schedules_removed"]] == ["Alpha Schedule"]
        mock_sync.assert_called_once()

        db_session.expire_all()
        refreshed = await db_session.get(Device, device_id)
        assert refreshed.group_id is None
        assert await _device_memberships(db_session, device_id) == set()

    async def test_replace_memberships_dry_run_previews_without_mutating(self, client, db_session):
        group_a = await _create_group(db_session, "Alpha")
        group_b = await _create_group(db_session, "Beta")
        await _create_schedule(db_session, group_id=group_a.id, name="Alpha Schedule")
        await _create_schedule(db_session, group_id=group_b.id, name="Beta Schedule")
        device = await _create_device(
            db_session,
            device_id="g2m-replace-001",
            group_id=group_a.id,
        )

        with patch("cms.routers.devices.push_sync_to_device", new_callable=AsyncMock) as mock_sync:
            resp = await client.put(
                f"/api/devices/{device.id}/groups?dry_run=true",
                json={"group_ids": [str(group_b.id)]},
            )

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["dry_run"] is True
        assert data["changed"] is True
        assert data["current_group_ids"] == [str(group_a.id)]
        assert data["group_ids"] == [str(group_b.id)]
        assert [item["name"] for item in data["schedules_added"]] == ["Beta Schedule"]
        assert [item["name"] for item in data["schedules_removed"]] == ["Alpha Schedule"]
        mock_sync.assert_not_called()

        refreshed = await db_session.get(Device, device.id)
        assert refreshed.group_id == group_a.id
        assert await _device_memberships(db_session, device.id) == {group_a.id}

    async def test_replace_memberships_commits_and_syncs(self, client, db_session):
        group_a = await _create_group(db_session, "Alpha")
        group_b = await _create_group(db_session, "Beta")
        group_c = await _create_group(db_session, "Gamma")
        device = await _create_device(
            db_session,
            device_id="g2m-replace-002",
            group_id=group_a.id,
        )
        device_id = device.id

        with patch("cms.routers.devices.push_sync_to_device", new_callable=AsyncMock) as mock_sync:
            resp = await client.put(
                f"/api/devices/{device.id}/groups",
                json={"group_ids": [str(group_b.id), str(group_c.id)]},
            )

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["changed"] is True
        assert data["group_id"] == str(group_b.id)
        assert set(data["group_ids"]) == {str(group_b.id), str(group_c.id)}
        mock_sync.assert_called_once()

        db_session.expire_all()
        refreshed = await db_session.get(Device, device_id)
        assert refreshed.group_id == group_b.id
        assert await _device_memberships(db_session, device_id) == {group_b.id, group_c.id}

    async def test_add_membership_requires_manageable_group(self, app, db_session):
        group = await _create_group(db_session, "Restricted")
        device = await _create_device(
            db_session,
            device_id="g2m-auth-001",
        )
        limited_role = Role(
            name=f"LimitedWriter{uuid.uuid4().hex[:6]}",
            permissions=[DEVICES_READ, DEVICES_WRITE],
        )
        db_session.add(limited_role)
        await db_session.flush()
        await _create_user(
            db_session,
            email="limited-membership@test.com",
            role_id=limited_role.id,
            group_ids=[group.id],
        )
        client = await _login_as(app, "limited-membership@test.com")
        try:
            resp = await client.post(
                f"/api/devices/{device.id}/groups",
                json={"group_id": str(group.id)},
            )
            assert resp.status_code == 403
        finally:
            await client.aclose()

    async def test_replace_memberships_requires_authority_over_all_added_groups(
        self, app, db_session,
    ):
        group_a = await _create_group(db_session, "Alpha")
        group_b = await _create_group(db_session, "Beta")
        device = await _create_device(
            db_session,
            device_id="g2m-auth-002",
            group_id=group_a.id,
        )
        await _create_user(
            db_session,
            email="scoped-operator@test.com",
            role_name="Operator",
            group_ids=[group_a.id],
        )
        client = await _login_as(app, "scoped-operator@test.com")
        try:
            resp = await client.put(
                f"/api/devices/{device.id}/groups",
                json={"group_ids": [str(group_a.id), str(group_b.id)]},
            )
            assert resp.status_code == 403
        finally:
            await client.aclose()


@pytest.mark.asyncio
class TestPatchCompatibility:
    async def test_patch_group_id_still_works_and_returns_additive_groups(
        self, client, db_session,
    ):
        group = await _create_group(db_session, "Lobby")
        device = await _create_device(
            db_session,
            device_id="g2m-patch-001",
        )

        with patch("cms.routers.devices.push_sync_to_device", new_callable=AsyncMock) as mock_sync:
            resp = await client.patch(
                f"/api/devices/{device.id}",
                json={"group_id": str(group.id)},
            )

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["group_id"] == str(group.id)
        assert data["group_name"] == "Lobby"
        assert data["group_ids"] == [str(group.id)]
        assert data["groups"] == [{"id": str(group.id), "name": "Lobby"}]
        mock_sync.assert_called_once()

    async def test_patch_group_ids_replaces_memberships(self, client, db_session):
        group_a = await _create_group(db_session, "Alpha")
        group_b = await _create_group(db_session, "Beta")
        group_c = await _create_group(db_session, "Gamma")
        device = await _create_device(
            db_session,
            device_id="g2m-patch-002",
            group_id=group_a.id,
        )
        device_id = device.id

        with patch("cms.routers.devices.push_sync_to_device", new_callable=AsyncMock) as mock_sync:
            resp = await client.patch(
                f"/api/devices/{device.id}",
                json={"group_ids": [str(group_b.id), str(group_c.id)]},
            )

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["group_id"] == str(group_b.id)
        assert set(data["group_ids"]) == {str(group_b.id), str(group_c.id)}
        assert {group["id"] for group in data["groups"]} == {
            str(group_b.id),
            str(group_c.id),
        }
        mock_sync.assert_called_once()

        db_session.expire_all()
        refreshed = await db_session.get(Device, device_id)
        assert refreshed.group_id == group_b.id
        assert await _device_memberships(db_session, device_id) == {group_b.id, group_c.id}

    async def test_patch_rejects_group_id_and_group_ids_together(self, client, db_session):
        group_a = await _create_group(db_session, "Alpha")
        group_b = await _create_group(db_session, "Beta")
        device = await _create_device(
            db_session,
            device_id="g2m-patch-003",
        )

        resp = await client.patch(
            f"/api/devices/{device.id}",
            json={
                "group_id": str(group_a.id),
                "group_ids": [str(group_b.id)],
            },
        )

        assert resp.status_code == 422

    async def test_device_response_includes_groups_while_keeping_legacy_group_fields(
        self, client, db_session,
    ):
        group_a = await _create_group(db_session, "Alpha")
        group_b = await _create_group(db_session, "Beta")
        device = await _create_device(
            db_session,
            device_id="g2m-get-001",
            group_id=group_a.id,
        )
        db_session.add(DeviceGroupMembership(device_id=device.id, group_id=group_b.id))
        await db_session.commit()

        resp = await client.get(f"/api/devices/{device.id}")

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["group_id"] == str(group_a.id)
        assert data["group_name"] == "Alpha"
        assert set(data["group_ids"]) == {str(group_a.id), str(group_b.id)}
        assert {group["name"] for group in data["groups"]} == {"Alpha", "Beta"}
