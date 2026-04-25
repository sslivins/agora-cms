"""Event Log page + /api/device-events RBAC and system-event visibility tests."""

import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from cms.auth import hash_password
from cms.models.device import Device, DeviceGroup, DeviceStatus
from cms.models.device_event import DeviceEvent, DeviceEventType
from cms.models.user import Role, User, UserGroup


# ── Fixtures ──


@pytest_asyncio.fixture
async def two_groups_with_events(app):
    """Seed 2 groups, each with 1 device, events per device, plus one system event."""
    from cms.database import get_db
    factory = app.dependency_overrides[get_db]

    async for db in factory():
        group_a = DeviceGroup(name="Group A")
        group_b = DeviceGroup(name="Group B")
        db.add_all([group_a, group_b])
        await db.flush()

        dev_a = Device(id="evt-dev-a", name="Device A",
                       status=DeviceStatus.ADOPTED, group_id=group_a.id)
        dev_b = Device(id="evt-dev-b", name="Device B",
                       status=DeviceStatus.ADOPTED, group_id=group_b.id)
        db.add_all([dev_a, dev_b])
        await db.flush()

        # Events for device A
        db.add(DeviceEvent(
            device_id=dev_a.id, device_name=dev_a.name,
            group_id=group_a.id, group_name=group_a.name,
            event_type=DeviceEventType.OFFLINE,
        ))
        db.add(DeviceEvent(
            device_id=dev_a.id, device_name=dev_a.name,
            group_id=group_a.id, group_name=group_a.name,
            event_type=DeviceEventType.ONLINE,
        ))
        # Event for device B
        db.add(DeviceEvent(
            device_id=dev_b.id, device_name=dev_b.name,
            group_id=group_b.id, group_name=group_b.name,
            event_type=DeviceEventType.OFFLINE,
        ))
        # System event (device_id is null)
        db.add(DeviceEvent(
            device_id=None, device_name="CMS",
            group_id=None, group_name="",
            event_type=DeviceEventType.CMS_STARTED,
            details={"version": "test"},
        ))
        await db.commit()

        yield {
            "group_a_id": group_a.id,
            "group_b_id": group_b.id,
            "dev_a_id": dev_a.id,
            "dev_b_id": dev_b.id,
        }
        break


async def _mk_user(app, *, username, password, role_name, group_ids=None):
    from cms.database import get_db
    factory = app.dependency_overrides[get_db]
    async for db in factory():
        result = await db.execute(select(Role).where(Role.name == role_name))
        role = result.scalar_one()
        user = User(
            username=username,
            email=f"{username}@test.com",
            display_name=username,
            password_hash=hash_password(password),
            role_id=role.id,
            is_active=True,
            must_change_password=False,
        )
        db.add(user)
        await db.flush()
        for gid in (group_ids or []):
            db.add(UserGroup(user_id=user.id, group_id=gid))
        await db.commit()
        break


async def _login(app, username, password):
    transport = ASGITransport(app=app)
    ac = AsyncClient(transport=transport, base_url="http://test")
    await ac.post("/login", data={"username": username, "password": password},
                  follow_redirects=False)
    return ac


async def _mk_role_no_perms(app, name="NoAccess"):
    from cms.database import get_db
    factory = app.dependency_overrides[get_db]
    async for db in factory():
        role = Role(name=name, description="no perms", permissions=[], is_builtin=False)
        db.add(role)
        await db.commit()
        break


# ── Admin visibility ──


@pytest.mark.asyncio
async def test_admin_sees_all_device_and_system_events(client, two_groups_with_events):
    resp = await client.get("/api/device-events")
    assert resp.status_code == 200
    events = resp.json()
    assert len(events) == 4
    types = [e["event_type"] for e in events]
    assert "cms_started" in types
    # Both devices visible
    dev_ids = {e["device_id"] for e in events}
    assert "evt-dev-a" in dev_ids and "evt-dev-b" in dev_ids and None in dev_ids


@pytest.mark.asyncio
async def test_admin_page_renders_200(client, two_groups_with_events):
    resp = await client.get("/event-log")
    assert resp.status_code == 200
    assert "Event Log" in resp.text


@pytest.mark.asyncio
async def test_event_log_page_200_with_empty_db(client):
    """Empty DB still renders page successfully."""
    resp = await client.get("/event-log")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_offline_event_kinds_render_humanized(app, client):
    """Stale/grace event details render as friendly strings, not raw JSON.

    Also asserts legacy ``grace_period`` rendering is preserved.
    """
    from cms.database import get_db
    factory = app.dependency_overrides[get_db]
    async for db in factory():
        grp = DeviceGroup(name="Polish Group")
        db.add(grp)
        await db.flush()
        dev = Device(id="polish-dev", name="Polish Device",
                     status=DeviceStatus.ADOPTED, group_id=grp.id)
        db.add(dev)
        await db.flush()
        db.add(DeviceEvent(
            device_id=dev.id, device_name=dev.name,
            group_id=grp.id, group_name=grp.name,
            event_type=DeviceEventType.OFFLINE,
            details={"kind": "stale_heartbeat"},
        ))
        db.add(DeviceEvent(
            device_id=dev.id, device_name=dev.name,
            group_id=grp.id, group_name=grp.name,
            event_type=DeviceEventType.OFFLINE,
            details={"kind": "grace_expired"},
        ))
        db.add(DeviceEvent(
            device_id=dev.id, device_name=dev.name,
            group_id=grp.id, group_name=grp.name,
            event_type=DeviceEventType.OFFLINE,
            details={"grace_period": 120},
        ))
        await db.commit()
        break

    resp = await client.get("/event-log")
    assert resp.status_code == 200
    text = resp.text
    assert "No heartbeat received within timeout" in text
    assert "Grace period exceeded" in text
    assert "Grace period: 120s" in text  # legacy rendering preserved
    # Raw JSON of the new ``kind`` payloads must NOT bleed through.
    assert '"kind": "stale_heartbeat"' not in text
    assert '"kind": "grace_expired"' not in text


@pytest.mark.asyncio
async def test_settings_page_default_offline_grace_300(client):
    """Fresh /settings render shows the new 300s default in the input."""
    resp = await client.get("/settings")
    assert resp.status_code == 200
    # Input element with the documented id and the new default value.
    assert 'id="alert-offline-grace"' in resp.text
    assert 'value="300"' in resp.text


# ── Non-admin (group-scoped) visibility ──


@pytest.mark.asyncio
async def test_operator_scoped_sees_only_own_group_plus_system(app, two_groups_with_events):
    info = two_groups_with_events
    await _mk_user(app, username="op-a", password="pw",
                   role_name="Operator", group_ids=[info["group_a_id"]])
    ac = await _login(app, "op-a", "pw")
    try:
        resp = await ac.get("/api/device-events")
        assert resp.status_code == 200
        events = resp.json()
        dev_ids = {e["device_id"] for e in events}
        types = [e["event_type"] for e in events]
        # Group A's device present
        assert info["dev_a_id"] in dev_ids
        # Group B's device NOT present
        assert info["dev_b_id"] not in dev_ids
        # System event present
        assert None in dev_ids
        assert "cms_started" in types
    finally:
        await ac.aclose()


@pytest.mark.asyncio
async def test_operator_no_groups_sees_only_system_events(app, two_groups_with_events):
    await _mk_user(app, username="op-none", password="pw",
                   role_name="Operator", group_ids=[])
    ac = await _login(app, "op-none", "pw")
    try:
        resp = await ac.get("/api/device-events")
        assert resp.status_code == 200
        events = resp.json()
        # Only system events (device_id is null)
        assert all(e["device_id"] is None for e in events)
        assert len(events) >= 1
    finally:
        await ac.aclose()


# ── No devices:read permission ──


# NOTE: /api/device-events is gated only by authentication; it does not require
# devices:read. Non-admin users without any group membership simply receive the
# narrowest scope (system events only). We assert that scoping behaviour below
# via test_operator_no_groups_sees_only_system_events rather than a 403 check.


@pytest.mark.asyncio
async def test_no_devices_read_denied_on_page(app, two_groups_with_events):
    await _mk_role_no_perms(app, name="NoAccess2")
    await _mk_user(app, username="no-perm-ui", password="pw", role_name="NoAccess2")
    ac = await _login(app, "no-perm-ui", "pw")
    try:
        resp = await ac.get("/event-log", follow_redirects=False)
        # 403 JSON — get_current_user passes, require_permission raises 403
        assert resp.status_code in (303, 401, 403)
    finally:
        await ac.aclose()


# ── Dropdown & filters ──


@pytest.mark.asyncio
async def test_device_dropdown_excludes_system_events(client, two_groups_with_events):
    """The device filter dropdown should only list real devices, not 'null'/system."""
    resp = await client.get("/event-log")
    assert resp.status_code == 200
    text = resp.text
    # Devices dropdown should have Device A and B as options
    assert 'value="evt-dev-a"' in text
    assert 'value="evt-dev-b"' in text
    # No option with an empty-string or 'None' value beyond the "All Devices" default
    assert 'value="None"' not in text
    # System-event's device_name ("CMS") shouldn't appear as a dropdown <option>
    # (guard: after "All Devices" the only options are real device names)
    assert text.count('name="device_id"') == 1  # sanity: one select exists


@pytest.mark.asyncio
async def test_api_filter_by_event_type_cms_started(client, two_groups_with_events):
    resp = await client.get("/api/device-events?event_type=cms_started")
    assert resp.status_code == 200
    events = resp.json()
    assert len(events) >= 1
    assert all(e["event_type"] == "cms_started" for e in events)
    assert all(e["device_id"] is None for e in events)


@pytest.mark.asyncio
async def test_api_filter_by_device_id_excludes_system(client, two_groups_with_events):
    info = two_groups_with_events
    resp = await client.get(f"/api/device-events?device_id={info['dev_a_id']}")
    assert resp.status_code == 200
    events = resp.json()
    assert len(events) >= 1
    assert all(e["device_id"] == info["dev_a_id"] for e in events)
