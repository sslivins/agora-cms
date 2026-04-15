"""Tests for group icon visibility in the header.

Users with groups:view_all should not see the group membership icon
since they can access all groups regardless of explicit membership.
"""

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from cms.auth import hash_password
from cms.models.user import Role, User


async def _get_role_id(db, name):
    result = await db.execute(select(Role).where(Role.name == name))
    return result.scalar_one().id


async def _create_user(db, *, email, role_name, display_name=None):
    role_id = await _get_role_id(db, role_name)
    username = email.split("@")[0]
    user = User(
        username=username,
        email=email,
        display_name=display_name or username,
        password_hash=hash_password("password123"),
        role_id=role_id,
        is_active=True,
        must_change_password=False,
    )
    db.add(user)
    await db.commit()
    return user


async def _login_as(app, email):
    transport = ASGITransport(app=app)
    ac = AsyncClient(transport=transport, base_url="http://test")
    username = email.split("@")[0]
    await ac.post("/login", data={"username": username, "password": "password123"},
                  follow_redirects=False)
    return ac


@pytest.mark.asyncio
class TestHeaderGroupIcon:
    """The group membership icon should be hidden for users with groups:view_all."""

    async def test_admin_does_not_see_group_icon(self, client):
        """Admin has groups:view_all — header should not contain the group badge."""
        resp = await client.get("/")
        assert resp.status_code == 200
        html = resp.text
        assert "header-group-count" not in html

    async def test_operator_sees_group_icon(self, app, db_session):
        """Operator lacks groups:view_all — header should show the group badge."""
        operator = await _create_user(db_session, email="grpicon_op@test.com",
                                      role_name="Operator")
        op_client = await _login_as(app, "grpicon_op@test.com")
        try:
            resp = await op_client.get("/")
            assert resp.status_code == 200
            assert "header-group-count" in resp.text
        finally:
            await op_client.aclose()

    async def test_viewer_sees_group_icon(self, app, db_session):
        """Viewer lacks groups:view_all — header should show the group badge."""
        viewer = await _create_user(db_session, email="grpicon_vw@test.com",
                                    role_name="Viewer")
        vw_client = await _login_as(app, "grpicon_vw@test.com")
        try:
            resp = await vw_client.get("/")
            assert resp.status_code == 200
            assert "header-group-count" in resp.text
        finally:
            await vw_client.aclose()
