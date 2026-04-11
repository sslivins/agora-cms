"""Tests for Database infrastructure status endpoints."""

from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
class TestDbStatus:
    """Test the /api/db/status endpoint."""

    async def test_db_status_returns_valid_response(self, client):
        """DB status returns a response (offline in SQLite test env)."""
        resp = await client.get("/api/db/status")
        assert resp.status_code == 200
        data = resp.json()
        # SQLite doesn't support pg_ functions, so it'll return offline
        assert "online" in data

    async def test_settings_page_shows_db_card(self, client):
        resp = await client.get("/settings")
        assert resp.status_code == 200
        assert "Database" in resp.text
        assert "db-status-badge" in resp.text
        assert "Change Database Password" in resp.text


@pytest.mark.asyncio
class TestDbChangePassword:
    """Test the /api/db/change-password endpoint."""

    async def test_password_too_short(self, client):
        resp = await client.post(
            "/api/db/change-password",
            json={"password": "abc"},
        )
        assert resp.status_code == 400
        assert "at least 6 characters" in resp.json()["detail"]

    async def test_empty_password(self, client):
        resp = await client.post(
            "/api/db/change-password",
            json={"password": ""},
        )
        assert resp.status_code == 400

    async def test_change_password_fails_on_sqlite(self, client):
        """ALTER USER doesn't exist in SQLite — should return 500 gracefully."""
        resp = await client.post(
            "/api/db/change-password",
            json={"password": "new-secure-password"},
        )
        # SQLite doesn't support ALTER USER, so this should fail gracefully
        assert resp.status_code == 500
        assert "detail" in resp.json()
