"""Stage 4 RBAC tests for shared devices (#863)."""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from cms.auth import (
    assert_authority_over_group_set,
    can_manage_group_membership,
    hash_password,
)
from cms.models.device import Device, DeviceGroup, DeviceStatus
from cms.models.device_group_membership import DeviceGroupMembership
from cms.models.user import Role, User, UserGroup
from cms.permissions import (
    ALL_PERMISSIONS,
    BUILTIN_ROLES,
    DEVICES_READ,
    DEVICES_WRITE,
    GROUPS_VIEW_ALL,
    GROUPS_READ,
    SCHEDULES_READ,
    SCHEDULES_WRITE,
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
    role_name: str = "Viewer",
    role_id: uuid.UUID | None = None,
    group_ids: list[uuid.UUID] | None = None,
) -> User:
    """Create a test user with the given role and group assignments."""
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
    for gid in (group_ids or []):
        db.add(UserGroup(user_id=user.id, group_id=gid))
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
    group = DeviceGroup(name=name)
    db.add(group)
    await db.commit()
    await db.refresh(group)
    return group


@pytest.mark.asyncio
class TestSharedDeviceReadAccess:
    async def test_group_membership_any_of_allows_shared_device_read(
        self, app, db_session,
    ):
        """A user should reach a shared device through any authorised group."""
        group_a = await _create_group(db_session, "Shared Read A")
        group_b = await _create_group(db_session, "Shared Read B")
        device = Device(
            id="shared-read-01",
            name="Shared Read Device",
            status=DeviceStatus.ADOPTED,
        )
        db_session.add(device)
        await db_session.flush()
        db_session.add_all([
            DeviceGroupMembership(device_id=device.id, group_id=group_a.id),
            DeviceGroupMembership(device_id=device.id, group_id=group_b.id),
        ])
        await db_session.commit()

        await _create_user(
            db_session,
            email="shared_b@test.com",
            role_name="Operator",
            group_ids=[group_b.id],
        )
        client = await _login_as(app, "shared_b@test.com")
        try:
            resp = await client.get(f"/api/devices/{device.id}")
            assert resp.status_code == 200, resp.text
            assert resp.json()["id"] == device.id
        finally:
            await client.aclose()

    async def test_group_membership_any_of_still_denies_unshared_device(
        self, app, db_session,
    ):
        group_a = await _create_group(db_session, "Only A")
        group_b = await _create_group(db_session, "Only B")
        device = Device(
            id="group-a-only-01",
            name="Only A Device",
            status=DeviceStatus.ADOPTED,
        )
        db_session.add(device)
        await db_session.flush()
        db_session.add(DeviceGroupMembership(device_id=device.id, group_id=group_a.id))
        await db_session.commit()

        await _create_user(
            db_session,
            email="group_b_only@test.com",
            role_name="Operator",
            group_ids=[group_b.id],
        )
        client = await _login_as(app, "group_b_only@test.com")
        try:
            resp = await client.get(f"/api/devices/{device.id}")
            assert resp.status_code == 403
        finally:
            await client.aclose()


@pytest.mark.asyncio
class TestSharedDeviceListScoping:
    async def test_devices_api_uses_membership_rows_when_legacy_group_is_null(
        self, app, db_session,
    ):
        group_a = await _create_group(db_session, "Membership Visible")
        device = Device(
            id="membership-visible-01",
            name="Membership Visible Device",
            status=DeviceStatus.ADOPTED,
        )
        db_session.add(device)
        await db_session.flush()
        db_session.add(DeviceGroupMembership(device_id=device.id, group_id=group_a.id))
        await db_session.commit()

        await _create_user(
            db_session,
            email="membership_user@test.com",
            role_name="Operator",
            group_ids=[group_a.id],
        )
        scoped = await _login_as(app, "membership_user@test.com")
        try:
            resp = await scoped.get("/api/devices")
            assert resp.status_code == 200
            ids = {item["id"] for item in resp.json()}
            assert device.id in ids
        finally:
            await scoped.aclose()

        await _create_user(
            db_session,
            email="no_groups_user@test.com",
            role_name="Operator",
            group_ids=[],
        )
        unscoped = await _login_as(app, "no_groups_user@test.com")
        try:
            resp = await unscoped.get("/api/devices")
            assert resp.status_code == 200
            ids = {item["id"] for item in resp.json()}
            assert device.id not in ids, "membership-scoped device must not look ungrouped"
        finally:
            await unscoped.aclose()

    async def test_assert_authority_over_group_set_rejects_missing_group(
        self, db_session,
    ):
        group_a = await _create_group(db_session, "Authority A")
        group_b = await _create_group(db_session, "Authority B")
        role = Role(
            name=f"AuthorityRole{uuid.uuid4().hex[:6]}",
            permissions=[DEVICES_READ, DEVICES_WRITE, GROUPS_READ, SCHEDULES_READ, SCHEDULES_WRITE],
        )
        db_session.add(role)
        await db_session.flush()
        user = await _create_user(
            db_session,
            email="authority@test.com",
            role_id=role.id,
            group_ids=[group_a.id],
        )

        with pytest.raises(HTTPException) as exc_info:
            await assert_authority_over_group_set(user, db_session, {group_a.id, group_b.id})

        assert exc_info.value.status_code == 403

    async def test_can_manage_group_membership_requires_schedule_access(
        self, db_session,
    ):
        group = await _create_group(db_session, "Managed Group")
        limited_role = Role(
            name=f"DeviceWriter{uuid.uuid4().hex[:6]}",
            permissions=[DEVICES_READ, DEVICES_WRITE, GROUPS_READ],
        )
        db_session.add(limited_role)
        await db_session.flush()

        limited_user = await _create_user(
            db_session,
            email="limited_writer@test.com",
            role_id=limited_role.id,
            group_ids=[group.id],
        )
        assert await can_manage_group_membership(limited_user, db_session, group.id) is False

        full_role = Role(
            name=f"MembershipManager{uuid.uuid4().hex[:6]}",
            permissions=[DEVICES_READ, DEVICES_WRITE, GROUPS_READ, SCHEDULES_READ, SCHEDULES_WRITE],
        )
        db_session.add(full_role)
        await db_session.flush()
        operator = await _create_user(
            db_session,
            email="operator_manager@test.com",
            role_id=full_role.id,
            group_ids=[group.id],
        )
        assert await can_manage_group_membership(operator, db_session, group.id) is True

    async def test_device_group_replace_requires_schedule_authority(
        self, app, db_session,
    ):
        group = await _create_group(db_session, "Target Group")
        device = Device(
            id="replace-target-01",
            name="Replace Target",
            status=DeviceStatus.ADOPTED,
        )
        db_session.add(device)
        await db_session.commit()

        limited_role = Role(
            name=f"WriterNoSchedule{uuid.uuid4().hex[:6]}",
            permissions=[DEVICES_READ, DEVICES_WRITE, GROUPS_READ],
        )
        db_session.add(limited_role)
        await db_session.flush()
        await _create_user(
            db_session,
            email="no_schedule_writer@test.com",
            role_id=limited_role.id,
            group_ids=[group.id],
        )

        client = await _login_as(app, "no_schedule_writer@test.com")
        try:
            resp = await client.patch(
                f"/api/devices/{device.id}",
                json={"group_id": str(group.id)},
            )
            assert resp.status_code == 403
        finally:
            await client.aclose()

    async def test_operator_can_still_assign_single_group_device(
        self, app, db_session,
    ):
        group = await _create_group(db_session, "Operator Assignable")
        device = Device(
            id="single-group-assign-01",
            name="Assignable Device",
            status=DeviceStatus.ADOPTED,
        )
        db_session.add(device)
        await db_session.commit()

        await _create_user(
            db_session,
            email="assign_operator@test.com",
            role_name="Operator",
            group_ids=[group.id],
        )
        client = await _login_as(app, "assign_operator@test.com")
        try:
            resp = await client.patch(
                f"/api/devices/{device.id}",
                json={"group_id": str(group.id)},
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["group_id"] == str(group.id)
        finally:
            await client.aclose()

    async def test_admin_bypass_for_group_set_helper(
        self, db_session,
    ):
        group_a = await _create_group(db_session, "Admin Group A")
        group_b = await _create_group(db_session, "Admin Group B")
        admin_role = Role(
            name=f"ViewAllAdmin{uuid.uuid4().hex[:6]}",
            permissions=ALL_PERMISSIONS + [GROUPS_VIEW_ALL],
        )
        db_session.add(admin_role)
        await db_session.flush()
        admin = await _create_user(
            db_session,
            email="admin_helper@test.com",
            role_id=admin_role.id,
        )

        await assert_authority_over_group_set(admin, db_session, {group_a.id, group_b.id})
        assert await can_manage_group_membership(admin, db_session, group_b.id) is True
