"""Tests for the notification system: model, API, visibility, SMTP gate, resend invite."""

import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cms.auth import hash_password, set_setting
from cms.models.device import DeviceGroup
from cms.models.notification import Notification
from cms.models.user import Role, User, UserGroup


# ── Helpers ──


async def _get_role_id(db: AsyncSession, name: str) -> uuid.UUID:
    result = await db.execute(select(Role).where(Role.name == name))
    return result.scalar_one().id


async def _create_user(
    db: AsyncSession,
    *,
    email: str,
    role_name: str = "Viewer",
    group_ids: list | None = None,
    must_change_password: bool = False,
) -> User:
    role_id = await _get_role_id(db, role_name)
    username = email.split("@")[0]
    user = User(
        username=username,
        email=email,
        display_name=username,
        password_hash=hash_password("password123"),
        role_id=role_id,
        is_active=True,
        must_change_password=must_change_password,
    )
    db.add(user)
    await db.flush()
    for gid in (group_ids or []):
        db.add(UserGroup(user_id=user.id, group_id=gid))
    await db.commit()
    await db.refresh(user, ["role"])
    return user


async def _login_as(app, user_email: str) -> AsyncClient:
    transport = ASGITransport(app=app)
    ac = AsyncClient(transport=transport, base_url="http://test")
    username = user_email.split("@")[0]
    await ac.post(
        "/login",
        data={"username": username, "password": "password123"},
        follow_redirects=False,
    )
    return ac


async def _create_group(db: AsyncSession, name: str) -> DeviceGroup:
    group = DeviceGroup(name=name)
    db.add(group)
    await db.commit()
    await db.refresh(group)
    return group


async def _create_notification(
    db: AsyncSession,
    *,
    scope: str = "system",
    level: str = "info",
    title: str = "Test notification",
    message: str = "Test message",
    group_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
) -> Notification:
    notif = Notification(
        scope=scope,
        level=level,
        title=title,
        message=message,
        group_id=group_id,
        user_id=user_id,
    )
    db.add(notif)
    await db.commit()
    await db.refresh(notif)
    return notif


async def _setup_smtp(db: AsyncSession):
    """Configure SMTP settings in the test DB."""
    await set_setting(db, "smtp_host", "smtp.example.com")
    await set_setting(db, "smtp_from_email", "noreply@example.com")
    await db.commit()


# ── Notification visibility tests ──


@pytest.mark.asyncio
async def test_admin_sees_system_notifications(app, client, db_session):
    """Admin (has notifications:system perm) sees system-scoped notifications."""
    await _create_notification(db_session, scope="system", title="System alert")
    resp = await client.get("/api/notifications")
    assert resp.status_code == 200
    items = resp.json()
    assert any(n["title"] == "System alert" for n in items)


@pytest.mark.asyncio
async def test_viewer_cannot_see_system_notifications(app, db_session):
    """Viewer (no notifications:system perm) cannot see system-scoped notifications."""
    await _create_notification(db_session, scope="system", title="System alert")
    viewer = await _create_user(db_session, email="viewer@test.com", role_name="Viewer")
    viewer_client = await _login_as(app, "viewer@test.com")
    try:
        resp = await viewer_client.get("/api/notifications")
        assert resp.status_code == 200
        items = resp.json()
        assert not any(n["title"] == "System alert" for n in items)
    finally:
        await viewer_client.aclose()


@pytest.mark.asyncio
async def test_user_sees_own_user_notification(app, db_session):
    """User sees notifications scoped to their own user_id."""
    viewer = await _create_user(db_session, email="viewer2@test.com", role_name="Viewer")
    await _create_notification(
        db_session, scope="user", title="Your alert", user_id=viewer.id
    )
    viewer_client = await _login_as(app, "viewer2@test.com")
    try:
        resp = await viewer_client.get("/api/notifications")
        assert resp.status_code == 200
        items = resp.json()
        assert any(n["title"] == "Your alert" for n in items)
    finally:
        await viewer_client.aclose()


@pytest.mark.asyncio
async def test_user_cannot_see_other_users_notification(app, db_session):
    """User cannot see notifications targeted at another user."""
    viewer1 = await _create_user(db_session, email="v1@test.com", role_name="Viewer")
    await _create_user(db_session, email="v2@test.com", role_name="Viewer")
    await _create_notification(
        db_session, scope="user", title="Private", user_id=viewer1.id
    )
    v2_client = await _login_as(app, "v2@test.com")
    try:
        resp = await v2_client.get("/api/notifications")
        assert resp.status_code == 200
        items = resp.json()
        assert not any(n["title"] == "Private" for n in items)
    finally:
        await v2_client.aclose()


@pytest.mark.asyncio
async def test_group_member_sees_group_notification(app, db_session):
    """User in a group sees notifications scoped to that group."""
    group = await _create_group(db_session, "Lobby Screens")
    viewer = await _create_user(
        db_session, email="grp@test.com", role_name="Viewer", group_ids=[group.id]
    )
    await _create_notification(
        db_session, scope="group", title="Group alert", group_id=group.id
    )
    grp_client = await _login_as(app, "grp@test.com")
    try:
        resp = await grp_client.get("/api/notifications")
        assert resp.status_code == 200
        items = resp.json()
        assert any(n["title"] == "Group alert" for n in items)
    finally:
        await grp_client.aclose()


@pytest.mark.asyncio
async def test_non_member_cannot_see_group_notification(app, db_session):
    """User NOT in a group cannot see that group's notifications."""
    group = await _create_group(db_session, "Restricted Group")
    viewer = await _create_user(
        db_session, email="outsider@test.com", role_name="Viewer"
    )
    await _create_notification(
        db_session, scope="group", title="Group secret", group_id=group.id
    )
    outsider_client = await _login_as(app, "outsider@test.com")
    try:
        resp = await outsider_client.get("/api/notifications")
        assert resp.status_code == 200
        items = resp.json()
        assert not any(n["title"] == "Group secret" for n in items)
    finally:
        await outsider_client.aclose()


# ── Count and read endpoints ──


@pytest.mark.asyncio
async def test_unread_count(app, client, db_session):
    """Unread count endpoint returns the correct count."""
    await _create_notification(db_session, scope="system", title="N1")
    await _create_notification(db_session, scope="system", title="N2")
    resp = await client.get("/api/notifications/count")
    assert resp.status_code == 200
    assert resp.json()["unread"] == 2


@pytest.mark.asyncio
async def test_mark_read(app, client, db_session):
    """Marking a notification as read updates read_at."""
    notif = await _create_notification(db_session, scope="system", title="Read me")
    resp = await client.post(f"/api/notifications/{notif.id}/read")
    assert resp.status_code == 200
    data = resp.json()
    assert data["read_at"] is not None

    # Count should now be 0
    count_resp = await client.get("/api/notifications/count")
    assert count_resp.json()["unread"] == 0


@pytest.mark.asyncio
async def test_mark_all_read(app, client, db_session):
    """Mark-all-read clears unread count for visible notifications."""
    await _create_notification(db_session, scope="system", title="A1")
    await _create_notification(db_session, scope="system", title="A2")

    resp = await client.post("/api/notifications/read-all")
    assert resp.status_code == 200
    assert resp.json()["marked_read"] == 2

    count_resp = await client.get("/api/notifications/count")
    assert count_resp.json()["unread"] == 0


@pytest.mark.asyncio
async def test_unread_only_filter(app, client, db_session):
    """unread_only=true filters out already-read notifications."""
    n1 = await _create_notification(db_session, scope="system", title="Unread")
    n2 = await _create_notification(db_session, scope="system", title="WillBeRead")

    # Mark n2 as read
    await client.post(f"/api/notifications/{n2.id}/read")

    resp = await client.get("/api/notifications?unread_only=true")
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 1
    assert items[0]["title"] == "Unread"


# ── SMTP gate tests ──


@pytest.mark.asyncio
async def test_create_user_blocked_without_smtp(app, client, db_session):
    """Creating a user without SMTP configured returns 422."""
    role_id = await _get_role_id(db_session, "Viewer")
    resp = await client.post(
        "/api/users",
        json={
            "email": "newuser@test.com",
            "display_name": "New User",
            "role_id": str(role_id),
            "group_ids": [],
        },
    )
    assert resp.status_code == 422
    assert "SMTP" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_create_user_allowed_with_smtp(app, client, db_session):
    """Creating a user with SMTP configured succeeds (email send is mocked)."""
    await _setup_smtp(db_session)
    role_id = await _get_role_id(db_session, "Viewer")
    resp = await client.post(
        "/api/users",
        json={
            "email": "smtpuser@test.com",
            "display_name": "SMTP User",
            "role_id": str(role_id),
            "group_ids": [],
        },
    )
    # Should succeed (email is sent in background, may fail but user is created)
    assert resp.status_code == 201
    assert resp.json()["email"] == "smtpuser@test.com"


# ── Resend invite tests ──


@pytest.mark.asyncio
async def test_resend_invite_success(app, client, db_session):
    """Resend invite regenerates token and returns success."""
    await _setup_smtp(db_session)
    user = await _create_user(
        db_session, email="pending@test.com", role_name="Viewer",
        must_change_password=True,
    )
    old_token = user.setup_token

    resp = await client.post(f"/api/users/{user.id}/resend-invite")
    assert resp.status_code == 200
    assert "queued" in resp.json()["message"].lower() or "activation" in resp.json()["message"].lower()

    # Token should be regenerated
    await db_session.refresh(user)
    assert user.setup_token is not None
    assert user.setup_token != old_token


@pytest.mark.asyncio
async def test_resend_invite_blocked_without_smtp(app, client, db_session):
    """Resend invite without SMTP configured returns 422."""
    user = await _create_user(
        db_session, email="nosmtp@test.com", role_name="Viewer",
        must_change_password=True,
    )
    resp = await client.post(f"/api/users/{user.id}/resend-invite")
    assert resp.status_code == 422
    assert "SMTP" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_resend_invite_already_setup(app, client, db_session):
    """Resend invite for a user who already completed setup returns 400."""
    await _setup_smtp(db_session)
    user = await _create_user(
        db_session, email="setup@test.com", role_name="Viewer",
        must_change_password=False,
    )
    resp = await client.post(f"/api/users/{user.id}/resend-invite")
    assert resp.status_code == 400
    assert "already completed" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_resend_invite_not_found(app, client, db_session):
    """Resend invite for nonexistent user returns 404."""
    await _setup_smtp(db_session)
    fake_id = str(uuid.uuid4())
    resp = await client.post(f"/api/users/{fake_id}/resend-invite")
    assert resp.status_code == 404


# ── Auth: unauthenticated access ──


@pytest.mark.asyncio
async def test_notifications_require_auth(unauthed_client):
    """Notification endpoints require authentication."""
    resp = await unauthed_client.get("/api/notifications")
    assert resp.status_code in (401, 403, 307)

    resp = await unauthed_client.get("/api/notifications/count")
    assert resp.status_code in (401, 403, 307)
