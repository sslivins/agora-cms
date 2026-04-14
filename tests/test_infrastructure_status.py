"""Tests for Database infrastructure status endpoints."""

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

    async def test_settings_page_no_db_card(self, client):
        """Database card was removed from settings page."""
        resp = await client.get("/settings")
        assert resp.status_code == 200
        assert "db-status-badge" not in resp.text
        assert "Change Database Password" not in resp.text


@pytest.mark.asyncio
class TestDbChangePasswordRemoved:
    """Verify the dangerous DB change-password endpoint was removed."""

    async def test_change_password_endpoint_gone(self, client):
        resp = await client.post(
            "/api/db/change-password",
            json={"password": "new-secure-password"},
        )
        # Endpoint removed — should 404 or 405
        assert resp.status_code in (404, 405)
