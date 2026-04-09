"""Tests for NTP and Database infrastructure status endpoints."""

from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
class TestNtpStatus:
    """Test the /api/ntp/status endpoint."""

    async def test_ntp_offline_when_unreachable(self, client):
        """NTP returns offline when the container is not reachable."""
        with patch("socket.socket") as mock_socket_cls:
            mock_socket_cls.return_value.recvfrom.side_effect = OSError("timed out")
            resp = await client.get("/api/ntp/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["online"] is False

    async def test_ntp_online_when_reachable(self, client):
        """NTP returns online with details when server responds."""
        import struct
        import time

        # Build a fake NTP response
        ntp_epoch = 2208988800
        now = time.time()
        ntp_secs = int(now + ntp_epoch)
        ntp_frac = 0
        # NTP response: LI=0, VN=3, Mode=4(server), stratum=2
        response = struct.pack(
            "!BBBb11I",
            0x1C,  # LI=0, VN=3, Mode=4
            2,     # stratum
            6,     # poll
            -20,   # precision
            0, 0, 0, 0, 0, 0, 0, 0, 0,  # root delay, dispersion, ref id, timestamps
            ntp_secs, ntp_frac,  # transmit timestamp
        )

        mock_sock = AsyncMock()
        mock_sock.sendto = lambda *a: None
        mock_sock.recvfrom = lambda *a: (response, ("ntp", 123))
        mock_sock.settimeout = lambda *a: None
        mock_sock.close = lambda: None

        with patch("socket.socket", return_value=mock_sock):
            resp = await client.get("/api/ntp/status")

        assert resp.status_code == 200
        data = resp.json()
        assert data["online"] is True
        assert data["stratum"] == 2
        assert "offset_ms" in data

    async def test_settings_page_shows_ntp_card(self, client):
        resp = await client.get("/settings")
        assert resp.status_code == 200
        assert "NTP Server" in resp.text
        assert "ntp-status-badge" in resp.text


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
