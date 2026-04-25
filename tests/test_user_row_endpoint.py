"""Unit test for GET /api/users/{id}/row — the HTML fragment endpoint
used by the /users page's no-reload flows (create, update, toggle
active) and the cross-session poller. See issue #87."""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cms.models.user import Role


async def _get_role_id(db: AsyncSession, name: str) -> uuid.UUID:
    r = await db.execute(select(Role).where(Role.name == name))
    return r.scalar_one().id


async def _seed_smtp(db: AsyncSession):
    # create_user requires SMTP to be configured.
    from cms.auth import set_setting
    await set_setting(db, "smtp_host", "smtp.example.com")
    await set_setting(db, "smtp_from_email", "noreply@example.com")
    await db.commit()


@pytest.mark.asyncio
class TestUserRowEndpoint:
    async def test_row_returns_html_fragment(self, client, db_session):
        await _seed_smtp(db_session)
        role_id = str(await _get_role_id(db_session, "Viewer"))

        create = await client.post("/api/users", json={
            "email": "rowfrag@test.com",
            "display_name": "Row Fragment",
            "role_id": role_id,
            "group_ids": [],
        })
        assert create.status_code == 201, create.text
        user_id = create.json()["id"]

        resp = await client.get(f"/api/users/{user_id}/row")
        assert resp.status_code == 200
        body = resp.text
        # The <tr> carries the user id so the poller can locate it.
        assert f'data-user-id="{user_id}"' in body
        # Email and display name are rendered verbatim.
        assert "rowfrag@test.com" in body
        assert "Row Fragment" in body
        # Role badge appears.
        assert "Viewer" in body

    async def test_row_unknown_id_404(self, client):
        resp = await client.get(
            "/api/users/00000000-0000-0000-0000-000000000000/row"
        )
        assert resp.status_code == 404
