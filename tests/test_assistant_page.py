"""Tests for the /assistant HTML page (PR 5).

The endpoint is allowlist-gated: users not on the assistant allowlist
get a 404 (same shape as the /api/chat/* endpoints).  Admins always
pass via the existing escape hatch in assistant_enabled_for().
"""

from __future__ import annotations

import uuid

import pytest


async def _enable_for(app, user_id: uuid.UUID) -> None:
    from cms.database import get_db
    from cms.services.assistant_flag import set_allowlist
    factory = app.dependency_overrides[get_db]
    async for db in factory():
        await set_allowlist(db, [user_id])
        break


async def _disable_all(app) -> None:
    from cms.database import get_db
    from cms.services.assistant_flag import set_allowlist
    factory = app.dependency_overrides[get_db]
    async for db in factory():
        await set_allowlist(db, [])
        break


@pytest.mark.asyncio
class TestAssistantPage:
    async def test_admin_can_view(self, client):
        resp = await client.get("/assistant")
        assert resp.status_code == 200
        body = resp.text
        # Sanity-check the shell renders.
        assert "assistant-app" in body
        assert "/static/assistant.js" in body

    async def test_operator_off_allowlist_gets_404(self, operator_client, app):
        await _disable_all(app)
        resp = await operator_client.get("/assistant")
        assert resp.status_code == 404

    async def test_operator_on_allowlist_can_view(self, operator_client, app):
        await _enable_for(app, operator_client.user_id)
        resp = await operator_client.get("/assistant")
        assert resp.status_code == 200
        assert "assistant-app" in resp.text

    async def test_nav_tab_appears_when_enabled(self, operator_client, app):
        # Pull any HTML page (dashboard) and confirm the nav link is
        # only present when the feature is enabled for the user.
        await _disable_all(app)
        page = await operator_client.get("/")
        assert page.status_code == 200
        assert ">Assistant<" not in page.text

        await _enable_for(app, operator_client.user_id)
        page = await operator_client.get("/")
        assert page.status_code == 200
        assert ">Assistant<" in page.text
