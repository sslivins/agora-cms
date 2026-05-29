"""Tests for the Assistant chat API skeleton (PR 2 of 6).

Covers the feature-flag gating + thread CRUD round-trips. Does NOT
exercise the agent loop, MCP integration, SSE, or write-tool
approvals — those land in subsequent PRs.
"""

from __future__ import annotations

import json
import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select


@pytest_asyncio.fixture
async def operator_client(app):
    """Authenticated client for a non-admin Operator user.

    Used to exercise the allowlist gate: operators have no
    ``settings:write`` so the admin escape hatch doesn't apply.
    """
    from cms.auth import hash_password
    from cms.database import get_db
    from cms.models.user import Role, User

    factory = app.dependency_overrides[get_db]
    async for db in factory():
        role = (
            await db.execute(select(Role).where(Role.name == "Operator"))
        ).scalar_one()
        user = User(
            username="chat-operator",
            email="chat-op@test.com",
            display_name="Chat Operator",
            password_hash=hash_password("oppass"),
            role_id=role.id,
            is_active=True,
            must_change_password=False,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        operator_id = user.id
        break

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        await ac.post(
            "/login",
            data={"username": "chat-operator", "password": "oppass"},
            follow_redirects=False,
        )
        ac.user_id = operator_id  # type: ignore[attr-defined]
        yield ac


async def _enable_for(app, user_id: uuid.UUID) -> None:
    """Set the assistant allowlist to exactly ``[user_id]``."""
    from cms.database import get_db
    from cms.services.assistant_flag import set_allowlist

    factory = app.dependency_overrides[get_db]
    async for db in factory():
        await set_allowlist(db, [user_id])
        break


async def _disable_all(app) -> None:
    """Clear the assistant allowlist."""
    from cms.database import get_db
    from cms.services.assistant_flag import set_allowlist

    factory = app.dependency_overrides[get_db]
    async for db in factory():
        await set_allowlist(db, [])
        break


@pytest.mark.asyncio
class TestAssistantFeatureFlag:
    async def test_admin_bypass_always_enabled(self, client, app):
        await _disable_all(app)
        resp = await client.get("/api/chat/feature")
        assert resp.status_code == 200
        # settings:write escape hatch — admin sees feature regardless
        # of allowlist state.
        assert resp.json() == {"enabled": True}

    async def test_operator_disabled_by_default(self, operator_client, app):
        await _disable_all(app)
        resp = await operator_client.get("/api/chat/feature")
        assert resp.status_code == 200
        assert resp.json() == {"enabled": False}

    async def test_operator_disabled_endpoints_404(
        self, operator_client, app
    ):
        await _disable_all(app)
        # Threads list should look like the endpoint doesn't exist at all.
        assert (await operator_client.get("/api/chat/threads")).status_code == 404
        assert (
            await operator_client.post(
                "/api/chat/threads", json={"title": "x"}
            )
        ).status_code == 404

    async def test_operator_allowlisted_can_use(
        self, operator_client, app
    ):
        await _enable_for(app, operator_client.user_id)
        feature = await operator_client.get("/api/chat/feature")
        assert feature.json() == {"enabled": True}
        listing = await operator_client.get("/api/chat/threads")
        assert listing.status_code == 200
        assert listing.json() == []

    async def test_malformed_allowlist_setting_is_treated_as_empty(
        self, operator_client, app
    ):
        # Persist a deliberately broken value and confirm the operator
        # still sees the feature as disabled (no 500s into the router).
        from cms.auth import set_setting
        from cms.database import get_db
        from cms.services.assistant_flag import ASSISTANT_FLAG_KEY

        factory = app.dependency_overrides[get_db]
        async for db in factory():
            await set_setting(db, ASSISTANT_FLAG_KEY, "not json")
            break

        resp = await operator_client.get("/api/chat/feature")
        assert resp.status_code == 200
        assert resp.json() == {"enabled": False}


@pytest.mark.asyncio
class TestChatThreadCRUD:
    """Admin user (settings:write) → always enabled, so we drive these
    tests through the default ``client`` fixture without touching the
    allowlist."""

    async def test_create_list_messages_delete(self, client):
        created = await client.post("/api/chat/threads", json={"title": "Promo"})
        assert created.status_code == 201, created.text
        thread = created.json()
        assert thread["title"] == "Promo"
        tid = thread["id"]

        listing = (await client.get("/api/chat/threads")).json()
        assert any(t["id"] == tid for t in listing)

        msgs = await client.get(f"/api/chat/threads/{tid}/messages")
        assert msgs.status_code == 200
        assert msgs.json() == []

        deleted = await client.delete(f"/api/chat/threads/{tid}")
        assert deleted.status_code == 204
        listing2 = (await client.get("/api/chat/threads")).json()
        assert all(t["id"] != tid for t in listing2)

    async def test_create_with_empty_title_defaults_to_blank(self, client):
        resp = await client.post("/api/chat/threads", json={})
        assert resp.status_code == 201
        assert resp.json()["title"] == ""

    async def test_thread_isolation_cross_user(
        self, client, operator_client, app
    ):
        # Allowlist the operator and let it create a thread; the admin
        # (different user) must not see it in their listing or be able
        # to read its messages by ID.
        await _enable_for(app, operator_client.user_id)

        created = await operator_client.post(
            "/api/chat/threads", json={"title": "operator-only"}
        )
        assert created.status_code == 201
        op_thread_id = created.json()["id"]

        admin_listing = (await client.get("/api/chat/threads")).json()
        assert all(t["id"] != op_thread_id for t in admin_listing)

        # Cross-user fetch returns 404, not 403, so existence isn't
        # leaked.
        resp = await client.get(
            f"/api/chat/threads/{op_thread_id}/messages"
        )
        assert resp.status_code == 404
        # And admin cannot delete someone else's thread either.
        resp = await client.delete(f"/api/chat/threads/{op_thread_id}")
        assert resp.status_code == 404

    async def test_messages_404_for_unknown_thread(self, client):
        resp = await client.get(f"/api/chat/threads/{uuid.uuid4()}/messages")
        assert resp.status_code == 404

    async def test_delete_unknown_thread_404(self, client):
        resp = await client.delete(f"/api/chat/threads/{uuid.uuid4()}")
        assert resp.status_code == 404


@pytest.mark.asyncio
class TestAllowlistService:
    """Direct unit-style checks against the service layer (no HTTP)."""

    async def test_set_and_get_round_trip(self, app):
        from cms.database import get_db
        from cms.services.assistant_flag import (
            ASSISTANT_FLAG_KEY,
            get_allowlist,
            set_allowlist,
        )
        from cms.auth import get_setting

        u1, u2 = uuid.uuid4(), uuid.uuid4()
        factory = app.dependency_overrides[get_db]
        async for db in factory():
            await set_allowlist(db, [u1, u2, u1])  # de-duped
            got = await get_allowlist(db)
            assert got == [u1, u2]
            raw = await get_setting(db, ASSISTANT_FLAG_KEY)
            assert raw is not None
            assert json.loads(raw) == [str(u1), str(u2)]
            break

    async def test_invalid_uuids_in_setting_are_dropped(self, app):
        from cms.auth import set_setting
        from cms.database import get_db
        from cms.services.assistant_flag import (
            ASSISTANT_FLAG_KEY,
            get_allowlist,
        )

        u1 = uuid.uuid4()
        factory = app.dependency_overrides[get_db]
        async for db in factory():
            await set_setting(
                db,
                ASSISTANT_FLAG_KEY,
                json.dumps([str(u1), "not-a-uuid", 42]),
            )
            got = await get_allowlist(db)
            assert got == [u1]
            break
