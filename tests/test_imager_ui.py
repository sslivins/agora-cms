"""UI tests for the imager page (PR 5).

Verifies route registration, permission gating, and per-section
rendering in ``/imager``. The interactive behavior is exercised
indirectly via the API tests in ``test_imager_api.py``.
"""

from __future__ import annotations

import pytest

from tests.test_rbac import _create_user, _login_as


# ── Page-level access ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_admin_sees_full_page(client):
    """Default admin client sees both Build and Base Images sections."""
    resp = await client.get("/imager")
    assert resp.status_code == 200
    html = resp.text
    assert "Pi Image Provisioning" in html
    assert '<h3 style="margin-top:0;">Build Image</h3>' in html
    assert '<h3 style="margin:0;">Base Images</h3>' in html
    # Catalog modal markup is admin-only.
    assert "imagerCatalogModal" in html


@pytest.mark.asyncio
async def test_operator_sees_build_only(app, db_session):
    """Operator has imager:read + imager:build but not imager:manage."""
    await _create_user(db_session, email="op-img@test.com", role_name="Operator")
    ac = await _login_as(app, "op-img@test.com")
    try:
        resp = await ac.get("/imager")
        assert resp.status_code == 200
        html = resp.text
        assert '<h3 style="margin-top:0;">Build Image</h3>' in html
        assert '<h3 style="margin:0;">Base Images</h3>' not in html
    finally:
        await ac.aclose()


@pytest.mark.asyncio
async def test_viewer_denied(app, db_session):
    """Viewer has no imager permissions; page returns 403."""
    await _create_user(db_session, email="viewer-img-ui@test.com", role_name="Viewer")
    ac = await _login_as(app, "viewer-img-ui@test.com")
    try:
        resp = await ac.get("/imager")
        assert resp.status_code == 403
    finally:
        await ac.aclose()


@pytest.mark.asyncio
async def test_unauthed_redirects_to_login(unauthed_client):
    """Unauthenticated browser requests redirect to login; API requests get 401."""
    resp = await unauthed_client.get(
        "/imager", headers={"accept": "text/html"}, follow_redirects=False,
    )
    assert resp.status_code in (302, 303, 307)
    assert "/login" in resp.headers.get("location", "")


# ── Nav rendering ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_nav_link_present_for_admin(client):
    """Admin (has imager:read) sees the Imager nav tab on any page."""
    resp = await client.get("/")
    assert resp.status_code == 200
    assert 'href="/imager"' in resp.text


@pytest.mark.asyncio
async def test_nav_link_absent_for_viewer(app, db_session):
    """Viewer (no imager:read) does not see the Imager nav tab."""
    await _create_user(db_session, email="viewer-nav@test.com", role_name="Viewer")
    ac = await _login_as(app, "viewer-nav@test.com")
    try:
        resp = await ac.get("/")
        assert resp.status_code == 200
        assert 'href="/imager"' not in resp.text
    finally:
        await ac.aclose()
