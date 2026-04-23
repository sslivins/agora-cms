"""Tests for device log collection feature (post-PR-#345).

The synchronous ``POST /api/devices/{id}/logs`` RPC was retired for
multi-replica safety.  These tests now cover:

* the legacy endpoint is gone (404),
* the in-process log buffer that powers ``GET /api/cms/logs``,
* the over-the-wire ``RequestLogsMessage`` / ``LogsResponseMessage``
  schemas that firmware still speaks.

The new async flow (``POST /api/logs/requests`` + outbox + blob) has
its own coverage in ``tests/test_log_requests_api.py``,
``tests/test_log_drainer.py``, and ``tests/nightly/test_12_logs_roundtrip.py``.
"""

import pytest


# ── Legacy endpoint removal ──


class TestRetiredEndpoint:
    @pytest.mark.asyncio
    async def test_legacy_endpoint_returns_404(self, client):
        """``POST /api/devices/{id}/logs`` was retired in PR #345."""
        resp = await client.post(
            "/api/devices/any-id/logs", json={"since": "1h"},
        )
        assert resp.status_code == 404


# ── CMS-only log endpoint (unchanged, retains coverage here) ──


class TestCmsLogsEndpoint:
    @pytest.mark.asyncio
    async def test_cms_logs_returns_zip(self, client):
        """GET /api/cms/logs returns a zip containing cms/cms.log."""
        import io
        import zipfile

        resp = await client.get("/api/cms/logs")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/zip"
        assert "agora-cms-logs-" in resp.headers.get("content-disposition", "")

        zf = zipfile.ZipFile(io.BytesIO(resp.content))
        assert "cms/cms.log" in zf.namelist()


# ── Protocol message tests ──


class TestProtocolMessages:
    def test_request_logs_message(self):
        from cms.schemas.protocol import RequestLogsMessage

        msg = RequestLogsMessage(
            request_id="test-123",
            services=["agora-player"],
            since="6h",
        )
        data = msg.model_dump(mode="json")
        assert data["type"] == "request_logs"
        assert data["request_id"] == "test-123"
        assert data["services"] == ["agora-player"]
        assert data["since"] == "6h"
        assert data["protocol_version"] == 2

    def test_logs_response_message(self):
        from cms.schemas.protocol import LogsResponseMessage

        msg = LogsResponseMessage(
            request_id="test-123",
            device_id="dev-1",
            logs={"agora-player": "some log output"},
        )
        data = msg.model_dump(mode="json")
        assert data["type"] == "logs_response"
        assert data["logs"]["agora-player"] == "some log output"
        assert data["error"] is None

    def test_request_logs_defaults(self):
        from cms.schemas.protocol import RequestLogsMessage

        msg = RequestLogsMessage(request_id="test-456")
        assert msg.services is None
        assert msg.since == "24h"
