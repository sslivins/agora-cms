"""Stage 5 tests for alert / notification / event many-to-many behavior."""

from __future__ import annotations

import asyncio
import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from cms.models.device import Device, DeviceGroup, DeviceStatus
from cms.models.device_alert import DeviceAlert
from cms.models.device_event import DeviceEvent, DeviceEventType
from cms.models.device_group_membership import DeviceGroupMembership
from cms.models.notification import Notification, NotificationRead
from cms.services.alert_service import AlertService
from cms.services.device_events import emit_device_event
from cms.services.device_membership import set_single_group_membership
from tests.test_notifications import (
    _create_group,
    _create_notification,
    _create_user,
    _login_as,
)


@pytest.mark.asyncio
async def test_group_notification_read_and_dismiss_are_per_user(app, db_session):
    group_a = await _create_group(db_session, "Notif A")
    group_b = await _create_group(db_session, "Notif B")
    await _create_user(
        db_session, email="notif_a@test.com", role_name="Operator", group_ids=[group_a.id]
    )
    await _create_user(
        db_session, email="notif_b@test.com", role_name="Operator", group_ids=[group_b.id]
    )
    notification = await _create_notification(
        db_session,
        scope="group",
        title="Shared group alert",
        group_ids=[group_a.id, group_b.id],
    )

    alice = await _login_as(app, "notif_a@test.com")
    bob = await _login_as(app, "notif_b@test.com")
    try:
        resp = await alice.post(f"/api/notifications/{notification.id}/read")
        assert resp.status_code == 200
        assert resp.json()["read_at"] is not None

        resp = await alice.get("/api/notifications/count")
        assert resp.json()["unread"] == 0

        resp = await bob.get("/api/notifications/count")
        assert resp.json()["unread"] == 1

        resp = await alice.delete(f"/api/notifications/{notification.id}")
        assert resp.status_code == 200

        resp = await alice.get("/api/notifications")
        assert all(row["id"] != str(notification.id) for row in resp.json())

        resp = await bob.get("/api/notifications")
        shared = [row for row in resp.json() if row["id"] == str(notification.id)]
        assert len(shared) == 1
        assert shared[0]["read_at"] is None
    finally:
        await alice.aclose()
        await bob.aclose()


@pytest.mark.asyncio
async def test_mark_all_read_only_touches_visible_unread_notifications(app, db_session):
    group_a = await _create_group(db_session, "MarkAll A")
    group_b = await _create_group(db_session, "MarkAll B")
    user = await _create_user(
        db_session, email="mark_all@test.com", role_name="Operator", group_ids=[group_a.id]
    )
    visible_group = await _create_notification(
        db_session, scope="group", title="Visible group", group_ids=[group_a.id]
    )
    visible_user = await _create_notification(
        db_session, scope="user", title="Visible user", user_id=user.id
    )
    hidden_group = await _create_notification(
        db_session, scope="group", title="Hidden group", group_ids=[group_b.id]
    )

    client = await _login_as(app, "mark_all@test.com")
    try:
        resp = await client.post("/api/notifications/read-all")
        assert resp.status_code == 200
        assert resp.json()["marked_read"] == 2
    finally:
        await client.aclose()

    reads = (
        await db_session.execute(
            select(NotificationRead).where(NotificationRead.user_id == user.id)
        )
    ).scalars().all()
    assert {row.notification_id for row in reads} == {visible_group.id, visible_user.id}
    assert all(row.read_at is not None for row in reads)

    await db_session.refresh(hidden_group)
    assert hidden_group.read_at is None


@pytest.mark.asyncio
async def test_deleting_device_preserves_historical_device_events(db_session):
    group = await _create_group(db_session, "Durable Group")
    device = Device(
        id="durable-device-01",
        name="Durable Device",
        status=DeviceStatus.ADOPTED,
        group_id=group.id,
    )
    db_session.add(device)
    await db_session.flush()
    await set_single_group_membership(db_session, device.id, group.id)
    event = await emit_device_event(
        db_session,
        device_id=device.id,
        device_name=device.name,
        primary_group_id=group.id,
        primary_group_name=group.name,
        event_type=DeviceEventType.OFFLINE,
    )
    await db_session.commit()
    event_id = event.id

    await db_session.delete(device)
    await db_session.commit()

    db_session.expire_all()
    durable = await db_session.get(DeviceEvent, event_id)
    assert durable is not None
    assert durable.device_id is None
    assert durable.device_name == "Durable Device"


@pytest.mark.asyncio
async def test_deleting_group_only_removes_that_notification_link(app, db_session):
    group_a = await _create_group(db_session, "DeleteGroup A")
    group_b = await _create_group(db_session, "DeleteGroup B")
    group_b_id = group_b.id
    await _create_user(
        db_session, email="keep_group_b@test.com", role_name="Operator", group_ids=[group_b_id]
    )
    notification = await _create_notification(
        db_session,
        scope="group",
        title="Shared targets",
        group_ids=[group_a.id, group_b_id],
    )
    notification_id = notification.id

    await db_session.delete(group_a)
    await db_session.commit()

    db_session.expire_all()
    reloaded = (
        await db_session.execute(
            select(Notification)
            .options(selectinload(Notification.group_targets))
            .where(Notification.id == notification_id)
        )
    ).scalar_one()
    assert reloaded is not None
    assert set(reloaded.target_group_ids) == {group_b_id}

    client = await _login_as(app, "keep_group_b@test.com")
    try:
        resp = await client.get("/api/notifications")
        assert any(row["id"] == str(notification_id) for row in resp.json())
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_emitted_device_event_snapshots_all_groups_and_visibility(app, db_session):
    group_a = await _create_group(db_session, "Event A")
    group_b = await _create_group(db_session, "Event B")
    device = Device(
        id="event-m2m-01",
        name="Event M2M Device",
        status=DeviceStatus.ADOPTED,
        group_id=group_a.id,
    )
    db_session.add(device)
    await db_session.flush()
    await set_single_group_membership(db_session, device.id, group_a.id)
    db_session.add(DeviceGroupMembership(device_id=device.id, group_id=group_b.id))
    await db_session.commit()

    event = await emit_device_event(
        db_session,
        device_id=device.id,
        device_name=device.name,
        primary_group_id=group_a.id,
        primary_group_name=group_a.name,
        event_type=DeviceEventType.ERROR,
        details={"error": "boom"},
    )
    await db_session.commit()
    await db_session.refresh(event)

    assert set(str(gid) for gid in event.group_ids) == {str(group_a.id), str(group_b.id)}

    await _create_user(
        db_session, email="event_b@test.com", role_name="Operator", group_ids=[group_b.id]
    )
    client = await _login_as(app, "event_b@test.com")
    try:
        resp = await client.get("/api/device-events")
        assert resp.status_code == 200
        ids = {row["id"] for row in resp.json()}
        assert str(event.id) in ids
        resp = await client.get(f"/api/device-events?group_id={group_b.id}")
        assert any(row["id"] == str(event.id) for row in resp.json())
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_single_group_notification_and_event_shape_stays_compatible(app, db_session):
    group = await _create_group(db_session, "Compat Group")
    user = await _create_user(
        db_session, email="compat@test.com", role_name="Operator", group_ids=[group.id]
    )
    notification = await _create_notification(
        db_session, scope="group", title="Compat notif", group_id=group.id
    )

    device = Device(
        id="compat-device-01",
        name="Compat Device",
        status=DeviceStatus.ADOPTED,
        group_id=group.id,
    )
    db_session.add(device)
    await db_session.flush()
    await set_single_group_membership(db_session, device.id, group.id)
    await emit_device_event(
        db_session,
        device_id=device.id,
        device_name=device.name,
        primary_group_id=group.id,
        primary_group_name=group.name,
        event_type=DeviceEventType.ONLINE,
    )
    await db_session.commit()

    client = await _login_as(app, "compat@test.com")
    try:
        notif_resp = await client.get("/api/notifications")
        notif = next(row for row in notif_resp.json() if row["id"] == str(notification.id))
        assert notif["group_id"] == str(group.id)
        assert notif["group_ids"] == [str(group.id)]

        event_resp = await client.get(f"/api/device-events?device_id={device.id}")
        event = event_resp.json()[0]
        assert event["group_id"] == str(group.id)
        assert event["group_name"] == group.name
        assert event["group_ids"] == [str(group.id)]
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_offline_alert_lifecycle_row_opens_and_resolves(app):
    from cms.database import get_db

    factory = app.dependency_overrides[get_db]
    group_id = None
    group_name = ""
    async for db in factory():
        group = DeviceGroup(name="Alert Lifecycle Group")
        device = Device(
            id="alert-life-01",
            name="Alert Lifecycle Device",
            status=DeviceStatus.ADOPTED,
            group_id=group.id,
        )
        db.add(group)
        await db.flush()
        db.add(device)
        await db.flush()
        await set_single_group_membership(db, device.id, group.id)
        await db.commit()
        group_id = group.id
        group_name = group.name
        break

    svc = AlertService()
    svc._offline_grace_seconds = 1
    svc.device_disconnected(
        "alert-life-01",
        "Alert Lifecycle Device",
        str(group_id),
        group_name,
        status="adopted",
    )
    await asyncio.sleep(0.1)
    await asyncio.sleep(1.2)
    async for db in factory():
        await svc.offline_sweep_once(db)
        break

    async for db in factory():
        row = (
            await db.execute(
                select(DeviceAlert).where(
                    DeviceAlert.device_id == "alert-life-01",
                    DeviceAlert.kind == "offline",
                )
            )
        ).scalar_one()
        incident_id = row.incident_id
        assert row.state == "open"
        assert row.raise_event_id is not None
        break

    svc.device_reconnected(
        "alert-life-01",
        "Alert Lifecycle Device",
        str(group_id),
        group_name,
        status="adopted",
    )
    await asyncio.sleep(0.3)

    async for db in factory():
        row = (
            await db.execute(
                select(DeviceAlert).where(
                    DeviceAlert.device_id == "alert-life-01",
                    DeviceAlert.kind == "offline",
                )
            )
        ).scalar_one()
        assert row.state == "resolved"
        assert row.resolve_event_id is not None
        assert row.incident_id == incident_id
        break
