"""API tests for the imager settings endpoints (PR 7).

Covers ``GET /api/imager/settings`` (IMAGER_READ) and
``PUT /api/imager/settings`` (IMAGER_MANAGE) -- the new admin-
configurable catalog URL surface that replaced the
``BASE_IMAGE_CATALOG_URL`` env var.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select

from cms.auth import get_settings
from cms.models.audit_log import AuditLog
from cms.services.imager_settings import get_catalog_url, set_catalog_url

from tests.test_rbac import _create_user, _login_as


@pytest.fixture
def imager_settings(app):
    """Configure the singleton settings for imager tests.

    Mirrors :func:`tests.test_imager_api.imager_settings` so this file
    is self-contained.  Restores defaults on teardown.
    """
    settings = app.dependency_overrides[get_settings]()
    saved = {
        "base_image_allowed_hosts": settings.base_image_allowed_hosts,
        "base_url": settings.base_url,
    }
    settings.base_image_allowed_hosts = "github.com,objects.githubusercontent.com"
    settings.base_url = "https://cms.example.com"
    yield settings
    for k, v in saved.items():
        setattr(settings, k, v)


@pytest_asyncio.fixture
async def seeded_url(db_session):
    """Pre-seed a valid catalog URL in the DB."""
    url = (
        "https://github.com/sslivins/agora/releases/download/v1.11.28/catalog.json"
    )
    await set_catalog_url(db_session, url)
    await db_session.commit()
    return url


# -- GET /settings -----------------------------------------------------


@pytest.mark.asyncio
async def test_get_settings_unset_returns_null_url(client, imager_settings):
    """Default state: setting unset → ``catalog_url`` is null."""
    resp = await client.get("/api/imager/settings")
    assert resp.status_code == 200
    assert resp.json() == {"catalog_url": None}


@pytest.mark.asyncio
async def test_get_settings_returns_configured_url(
    client, imager_settings, seeded_url
):
    resp = await client.get("/api/imager/settings")
    assert resp.status_code == 200
    assert resp.json() == {"catalog_url": seeded_url}


@pytest.mark.asyncio
async def test_get_settings_allowed_for_operator(
    app, db_session, imager_settings, seeded_url
):
    """IMAGER_READ is enough to fetch settings (operator role)."""
    await _create_user(
        db_session, email="op-settings@test.com", role_name="Operator"
    )
    ac = await _login_as(app, "op-settings@test.com")
    try:
        resp = await ac.get("/api/imager/settings")
        assert resp.status_code == 200
        assert resp.json() == {"catalog_url": seeded_url}
    finally:
        await ac.aclose()


@pytest.mark.asyncio
async def test_get_settings_denied_for_viewer(
    app, db_session, imager_settings
):
    await _create_user(
        db_session, email="viewer-settings@test.com", role_name="Viewer"
    )
    ac = await _login_as(app, "viewer-settings@test.com")
    try:
        resp = await ac.get("/api/imager/settings")
        assert resp.status_code == 403
    finally:
        await ac.aclose()


# -- PUT /settings -----------------------------------------------------


@pytest.mark.asyncio
async def test_put_settings_admin_succeeds_and_persists(
    client, db_session, imager_settings
):
    new_url = (
        "https://github.com/sslivins/agora/releases/download/v1.12.0/catalog.json"
    )
    resp = await client.put(
        "/api/imager/settings", json={"catalog_url": new_url}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"catalog_url": new_url}
    # Round-trip: GET sees the new value.
    resp = await client.get("/api/imager/settings")
    assert resp.json() == {"catalog_url": new_url}
    # And it really is in the DB.
    await db_session.commit()  # make sure we see the latest committed view
    assert await get_catalog_url(db_session) == new_url


@pytest.mark.asyncio
async def test_put_settings_denied_for_operator(
    app, db_session, imager_settings
):
    await _create_user(
        db_session, email="op-put@test.com", role_name="Operator"
    )
    ac = await _login_as(app, "op-put@test.com")
    try:
        resp = await ac.put(
            "/api/imager/settings",
            json={
                "catalog_url": (
                    "https://github.com/sslivins/agora/releases/"
                    "download/v1.11.28/catalog.json"
                )
            },
        )
        assert resp.status_code == 403
    finally:
        await ac.aclose()


@pytest.mark.asyncio
async def test_put_settings_denied_for_viewer(
    app, db_session, imager_settings
):
    await _create_user(
        db_session, email="viewer-put@test.com", role_name="Viewer"
    )
    ac = await _login_as(app, "viewer-put@test.com")
    try:
        resp = await ac.put(
            "/api/imager/settings",
            json={
                "catalog_url": (
                    "https://github.com/sslivins/agora/releases/"
                    "download/v1.11.28/catalog.json"
                )
            },
        )
        assert resp.status_code == 403
    finally:
        await ac.aclose()


@pytest.mark.asyncio
async def test_put_settings_rejects_http(client, imager_settings):
    resp = await client.put(
        "/api/imager/settings",
        json={
            "catalog_url": (
                "http://github.com/sslivins/agora/releases/"
                "download/v1.11.28/catalog.json"
            )
        },
    )
    assert resp.status_code == 422
    assert "https" in resp.text.lower()


@pytest.mark.asyncio
async def test_put_settings_rejects_disallowed_host(client, imager_settings):
    resp = await client.put(
        "/api/imager/settings",
        json={"catalog_url": "https://evil.example.com/catalog.json"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_put_settings_rejects_blank(client, imager_settings):
    """Pydantic min_length=1 + service-side strip rejects empty/whitespace."""
    resp = await client.put(
        "/api/imager/settings", json={"catalog_url": ""}
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_put_settings_audit_logged(client, db_session, imager_settings):
    new_url = (
        "https://github.com/sslivins/agora/releases/download/v1.12.0/catalog.json"
    )
    resp = await client.put(
        "/api/imager/settings", json={"catalog_url": new_url}
    )
    assert resp.status_code == 200, resp.text
    rows = (
        await db_session.execute(
            select(AuditLog).where(AuditLog.action == "imager.settings.update")
        )
    ).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.resource_type == "imager_settings"
    assert row.resource_id == "catalog_url"
    assert row.details == {"catalog_url": new_url}
