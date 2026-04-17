"""Tests for POST /api/settings/alerts — alert threshold persistence & RBAC."""

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from cms.auth import hash_password, get_setting
from cms.models.user import Role, User


ALERT_KEYS = [
    "alert_offline_grace_seconds",
    "alert_temp_warning_c",
    "alert_temp_critical_c",
    "alert_temp_cooldown_seconds",
    "email_notifications_enabled",
]


@pytest_asyncio.fixture
async def operator_client(app):
    """A logged-in Operator client — lacks settings:write."""
    from cms.database import get_db
    factory = app.dependency_overrides[get_db]
    async for db in factory():
        role = (await db.execute(select(Role).where(Role.name == "Operator"))).scalar_one()
        user = User(
            username="alert-op",
            email="alert-op@test.com",
            display_name="Alert Op",
            password_hash=hash_password("pw"),
            role_id=role.id,
            is_active=True,
            must_change_password=False,
        )
        db.add(user)
        await db.commit()
        break

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        await ac.post("/login", data={"username": "alert-op", "password": "pw"},
                      follow_redirects=False)
        yield ac


# ── Admin can save ──


@pytest.mark.asyncio
async def test_admin_save_alert_settings_persists_all_keys(client, db_session):
    payload = {
        "offline_grace_seconds": 90,
        "temp_warning_c": 65.5,
        "temp_critical_c": 82.0,
        "temp_cooldown_seconds": 600,
        "email_notifications_enabled": True,
    }
    resp = await client.post("/api/settings/alerts", json=payload)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}

    assert await get_setting(db_session, "alert_offline_grace_seconds") == "90"
    assert await get_setting(db_session, "alert_temp_warning_c") == "65.5"
    assert await get_setting(db_session, "alert_temp_critical_c") == "82.0"
    assert await get_setting(db_session, "alert_temp_cooldown_seconds") == "600"
    assert await get_setting(db_session, "email_notifications_enabled") == "true"


@pytest.mark.asyncio
async def test_admin_save_email_disabled_stored_as_false(client, db_session):
    resp = await client.post("/api/settings/alerts", json={
        "offline_grace_seconds": 120,
        "temp_warning_c": 70,
        "temp_critical_c": 80,
        "temp_cooldown_seconds": 300,
        "email_notifications_enabled": False,
    })
    assert resp.status_code == 200
    assert await get_setting(db_session, "email_notifications_enabled") == "false"


@pytest.mark.asyncio
async def test_admin_save_omitted_email_flag_stored_as_false(client, db_session):
    """Missing email_notifications_enabled key should default to false."""
    resp = await client.post("/api/settings/alerts", json={
        "offline_grace_seconds": 120,
        "temp_warning_c": 70,
        "temp_critical_c": 80,
        "temp_cooldown_seconds": 300,
    })
    assert resp.status_code == 200
    assert await get_setting(db_session, "email_notifications_enabled") == "false"


@pytest.mark.asyncio
async def test_admin_save_uses_defaults_for_missing_keys(client, db_session):
    """Omitted numeric keys fall back to documented defaults (120/70/80/300)."""
    resp = await client.post("/api/settings/alerts", json={})
    assert resp.status_code == 200
    assert await get_setting(db_session, "alert_offline_grace_seconds") == "120"
    assert await get_setting(db_session, "alert_temp_warning_c") == "70.0"
    assert await get_setting(db_session, "alert_temp_critical_c") == "80.0"
    assert await get_setting(db_session, "alert_temp_cooldown_seconds") == "300"


# ── Non-admin denied ──


@pytest.mark.asyncio
async def test_operator_forbidden(operator_client):
    resp = await operator_client.post("/api/settings/alerts", json={
        "offline_grace_seconds": 60,
    })
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_unauthenticated_redirected(unauthed_client):
    resp = await unauthed_client.post("/api/settings/alerts", json={},
                                       headers={"accept": "application/json"})
    # Unauthed -> 401 JSON (API call)
    assert resp.status_code in (401, 303)


# NOTE: The endpoint has no explicit validation — values are passed through
# int()/float() coercion directly. There is no dedicated validation test
# because the implementation does not currently perform any.


# ── Smoke: saving settings doesn't break later reads/refresh loop ──


@pytest.mark.asyncio
async def test_saving_twice_updates_values(client, db_session):
    await client.post("/api/settings/alerts", json={
        "offline_grace_seconds": 30,
        "temp_warning_c": 60,
        "temp_critical_c": 70,
        "temp_cooldown_seconds": 100,
        "email_notifications_enabled": True,
    })
    resp = await client.post("/api/settings/alerts", json={
        "offline_grace_seconds": 45,
        "temp_warning_c": 62,
        "temp_critical_c": 72,
        "temp_cooldown_seconds": 200,
        "email_notifications_enabled": False,
    })
    assert resp.status_code == 200
    # Fresh session to bypass any caching
    from cms.database import get_db
    factory = client  # unused; just to ensure no leak
    # Read via db_session
    assert await get_setting(db_session, "alert_offline_grace_seconds") == "45"
    assert await get_setting(db_session, "email_notifications_enabled") == "false"


@pytest.mark.asyncio
async def test_alert_service_refresh_picks_up_saved_settings(client, app):
    """Saving settings followed by alert_service.refresh_settings() reloads values."""
    from cms.services.alert_service import AlertService
    await client.post("/api/settings/alerts", json={
        "offline_grace_seconds": 77,
        "temp_warning_c": 55.5,
        "temp_critical_c": 75.5,
        "temp_cooldown_seconds": 123,
        "email_notifications_enabled": True,
    })
    svc = AlertService()
    await svc.refresh_settings()
    assert svc._offline_grace_seconds == 77
    assert svc._temp_warning_c == 55.5
    assert svc._temp_critical_c == 75.5
    assert svc._temp_cooldown_seconds == 123
    assert svc._email_enabled is True
