"""Tests for the admin Assistant settings API (PR 6b).

Covers ``GET /api/settings/assistant`` and the two PUT endpoints that
the admin Settings UI uses.  Verifies RBAC (admin vs. operator),
input validation (unknown user_ids, malformed override keys), and
the reconciliation semantics of the budget PUT (incoming map is the
source of truth — overrides not in the body get cleared).
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from cms.auth import hash_password
from cms.models.user import Role, User
from cms.services.assistant.budget import (
    BUDGET_CAP_KEY,
    BUDGET_OVERRIDES_KEY,
    DEFAULT_DAILY_TOKEN_CAP,
    get_default_cap,
    get_overrides,
    set_default_cap,
    set_user_override,
)
from cms.services.assistant_flag import (
    ASSISTANT_FLAG_KEY,
    get_allowlist,
    set_allowlist,
)


@pytest_asyncio.fixture
async def operator_client(app):
    """A logged-in Operator client — lacks settings:write."""
    from cms.database import get_db

    factory = app.dependency_overrides[get_db]
    async for db in factory():
        role = (
            await db.execute(select(Role).where(Role.name == "Operator"))
        ).scalar_one()
        user = User(
            username="asst-op",
            email="asst-op@test.com",
            display_name="Asst Op",
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
        await ac.post(
            "/login",
            data={"username": "asst-op", "password": "pw"},
            follow_redirects=False,
        )
        yield ac


async def _admin_user(db):
    return (
        await db.execute(select(User).where(User.username == "admin"))
    ).scalar_one()


async def _make_extra_user(db, *, username="alice"):
    role = (
        await db.execute(select(Role).where(Role.name == "Operator"))
    ).scalar_one()
    u = User(
        username=username,
        email=f"{username}@test.com",
        display_name=username.title(),
        password_hash=hash_password("pw"),
        role_id=role.id,
        is_active=True,
        must_change_password=False,
    )
    db.add(u)
    await db.commit()
    await db.refresh(u)
    return u


class TestAssistantSettingsGet:
    @pytest.mark.asyncio
    async def test_default_state_returns_empty_allowlist_and_default_cap(
        self, client
    ):
        resp = await client.get("/api/settings/assistant")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["allowlist"] == []
        assert data["default_cap"] == DEFAULT_DAILY_TOKEN_CAP
        assert data["overrides"] == {}
        assert data["default_cap_fallback"] == DEFAULT_DAILY_TOKEN_CAP
        assert isinstance(data["users"], list)
        # admin user always present
        usernames = [u["username"] for u in data["users"]]
        assert "admin" in usernames

    @pytest.mark.asyncio
    async def test_returns_persisted_state(self, client, db_session):
        admin = await _admin_user(db_session)
        await set_allowlist(db_session, [admin.id])
        await set_default_cap(db_session, 12345)
        await set_user_override(db_session, admin.id, 99)
        await db_session.commit()

        resp = await client.get("/api/settings/assistant")
        assert resp.status_code == 200
        data = resp.json()
        assert data["allowlist"] == [str(admin.id)]
        assert data["default_cap"] == 12345
        assert data["overrides"] == {str(admin.id): 99}

    @pytest.mark.asyncio
    async def test_operator_forbidden(self, operator_client):
        resp = await operator_client.get("/api/settings/assistant")
        assert resp.status_code == 403


class TestAssistantAllowlistPut:
    @pytest.mark.asyncio
    async def test_admin_save_allowlist(self, client, db_session):
        admin = await _admin_user(db_session)
        alice = await _make_extra_user(db_session, username="alice")

        resp = await client.put(
            "/api/settings/assistant/allowlist",
            json={"user_ids": [str(admin.id), str(alice.id)]},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert set(data["allowlist"]) == {str(admin.id), str(alice.id)}

        stored = await get_allowlist(db_session)
        assert set(stored) == {admin.id, alice.id}

    @pytest.mark.asyncio
    async def test_empty_list_clears_allowlist(self, client, db_session):
        admin = await _admin_user(db_session)
        await set_allowlist(db_session, [admin.id])
        await db_session.commit()

        resp = await client.put(
            "/api/settings/assistant/allowlist",
            json={"user_ids": []},
        )
        assert resp.status_code == 200
        assert resp.json()["allowlist"] == []
        assert await get_allowlist(db_session) == []

    @pytest.mark.asyncio
    async def test_unknown_user_id_rejected(self, client):
        bogus = str(uuid.uuid4())
        resp = await client.put(
            "/api/settings/assistant/allowlist",
            json={"user_ids": [bogus]},
        )
        assert resp.status_code == 400
        body = resp.json()
        assert "unknown_user_ids" in body["detail"]
        assert bogus in body["detail"]["unknown_user_ids"]

    @pytest.mark.asyncio
    async def test_operator_forbidden(self, operator_client):
        resp = await operator_client.put(
            "/api/settings/assistant/allowlist", json={"user_ids": []}
        )
        assert resp.status_code == 403


class TestAssistantBudgetPut:
    @pytest.mark.asyncio
    async def test_admin_save_default_cap(self, client, db_session):
        resp = await client.put(
            "/api/settings/assistant/budget",
            json={"default_cap": 25000, "overrides": {}},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["default_cap"] == 25000
        assert await get_default_cap(db_session) == 25000

    @pytest.mark.asyncio
    async def test_admin_save_overrides_and_default(self, client, db_session):
        admin = await _admin_user(db_session)
        alice = await _make_extra_user(db_session, username="ovr-alice")

        resp = await client.put(
            "/api/settings/assistant/budget",
            json={
                "default_cap": 10000,
                "overrides": {str(admin.id): 5000, str(alice.id): 0},
            },
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["default_cap"] == 10000
        assert data["overrides"] == {str(admin.id): 5000, str(alice.id): 0}

        stored = await get_overrides(db_session)
        assert stored[admin.id] == 5000
        assert stored[alice.id] == 0

    @pytest.mark.asyncio
    async def test_overrides_reconciled_on_save(self, client, db_session):
        """Overrides not present in the PUT body get cleared."""
        admin = await _admin_user(db_session)
        alice = await _make_extra_user(db_session, username="rec-alice")

        await set_user_override(db_session, admin.id, 1000)
        await set_user_override(db_session, alice.id, 2000)
        await db_session.commit()

        # Send a PUT that includes ONLY admin's override.
        resp = await client.put(
            "/api/settings/assistant/budget",
            json={"default_cap": 50000, "overrides": {str(admin.id): 7777}},
        )
        assert resp.status_code == 200
        stored = await get_overrides(db_session)
        assert stored == {admin.id: 7777}
        assert alice.id not in stored

    @pytest.mark.asyncio
    async def test_negative_cap_allowed(self, client, db_session):
        """Negative = unlimited; the API must not reject it."""
        resp = await client.put(
            "/api/settings/assistant/budget",
            json={"default_cap": -1, "overrides": {}},
        )
        assert resp.status_code == 200
        assert resp.json()["default_cap"] == -1

    @pytest.mark.asyncio
    async def test_override_with_invalid_uuid_key_rejected(self, client):
        resp = await client.put(
            "/api/settings/assistant/budget",
            json={
                "default_cap": 5000,
                "overrides": {"not-a-uuid": 100},
            },
        )
        assert resp.status_code == 400
        assert "invalid_keys" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_override_for_unknown_user_rejected(self, client):
        bogus = str(uuid.uuid4())
        resp = await client.put(
            "/api/settings/assistant/budget",
            json={"default_cap": 5000, "overrides": {bogus: 1}},
        )
        assert resp.status_code == 400
        assert "unknown_user_ids" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_operator_forbidden(self, operator_client):
        resp = await operator_client.put(
            "/api/settings/assistant/budget",
            json={"default_cap": 1000, "overrides": {}},
        )
        assert resp.status_code == 403
