"""Unit test for GET /api/roles/{id}/card — the HTML fragment endpoint
used by the /users page Roles tab's no-reload flows (create, update)
and the cross-session poller. See issue #87."""

import pytest


@pytest.mark.asyncio
class TestRoleCardEndpoint:
    async def test_card_returns_html_fragment(self, client):
        create = await client.post("/api/roles", json={
            "name": "CardFragmentRole",
            "description": "role card fragment test",
            "permissions": ["devices:read"],
        })
        assert create.status_code == 201, create.text
        role_id = create.json()["id"]

        resp = await client.get(f"/api/roles/{role_id}/card")
        assert resp.status_code == 200
        body = resp.text
        # The card carries the role id so the poller can locate it.
        assert f'data-role-id="{role_id}"' in body
        # Name + description + permission tag render verbatim.
        assert "CardFragmentRole" in body
        assert "role card fragment test" in body
        assert "devices:read" in body

    async def test_card_builtin_role(self, client, db_session):
        # Built-in roles must render without the kebab menu actions.
        from sqlalchemy import select
        from cms.models.user import Role
        r = await db_session.execute(select(Role).where(Role.name == "Admin"))
        role = r.scalar_one()

        resp = await client.get(f"/api/roles/{role.id}/card")
        assert resp.status_code == 200
        body = resp.text
        assert "Built-in" in body
        # The kebab menu's Edit/Delete buttons shouldn't render for builtins.
        assert 'onclick="openEditRole' not in body
        assert 'onclick="deleteRole' not in body

    async def test_card_unknown_id_404(self, client):
        resp = await client.get(
            "/api/roles/00000000-0000-0000-0000-000000000000/card"
        )
        assert resp.status_code == 404
