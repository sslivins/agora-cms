"""Tests for the Assistant chat API skeleton (PR 2 of 6).

Covers the feature-flag gating + thread CRUD round-trips. Does NOT
exercise the agent loop, MCP integration, SSE, or write-tool
approvals — those land in subsequent PRs.
"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import select


async def _enable_for(app, user_id: uuid.UUID) -> None:
    """Set the assistant allowlist to exactly ``[user_id]``."""
    from cms.database import get_db
    from tests.assistant_helpers import set_assistant_allowlist as set_allowlist

    factory = app.dependency_overrides[get_db]
    async for db in factory():
        await set_allowlist(db, [user_id])
        break


async def _disable_all(app) -> None:
    """Clear the assistant allowlist."""
    from cms.database import get_db
    from tests.assistant_helpers import set_assistant_allowlist as set_allowlist

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

    async def test_unrecognised_stored_state_is_treated_as_the_default(
        self, operator_client, app
    ):
        # A row whose state string the code doesn't recognise (a downgrade
        # after a new state was added, say) must fall back to the declared
        # default rather than 500 the router.
        from cms.database import get_db
        from cms.models.feature_flag import FeatureFlagState

        factory = app.dependency_overrides[get_db]
        async for db in factory():
            db.add(FeatureFlagState(name="assistant", state="not-a-state"))
            await db.commit()
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
