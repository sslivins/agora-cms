"""Tests for the ``list_audit_events`` MCP tool (issue #292).

The MCP tools live in ``mcp/server.py`` which can't be imported from the CMS
test-suite (local ``mcp/`` package collides with the ``mcp`` pip dep).  We
therefore exercise the two halves that together define the tool's contract:

1. The REST endpoint the tool wraps (``GET /api/audit-log``).
2. The ``CMSClient.list_audit_events`` method it delegates to, using
   ``httpx.MockTransport`` so we never touch the network.

Permission handling (``audit:read`` required) is enforced at the router by
``require_permission(AUDIT_READ)`` and is covered by the endpoint tests.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import pytest_asyncio

from cms.models.audit_log import AuditLog


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _seed_events(db_session, count: int = 3, *, action: str = "asset.delete",
                       resource_type: str = "asset"):
    """Insert ``count`` audit entries with predictable timestamps."""
    base = datetime.now(timezone.utc) - timedelta(hours=count)
    ids: list[uuid.UUID] = []
    for i in range(count):
        entry = AuditLog(
            action=action,
            resource_type=resource_type,
            resource_id=f"res-{i}",
            description=f"deleted asset #{i}",
            created_at=base + timedelta(hours=i),
        )
        db_session.add(entry)
        await db_session.flush()
        ids.append(entry.id)
    await db_session.commit()
    return ids


# ---------------------------------------------------------------------------
# REST endpoint tests — the tool's server-side contract
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestAuditLogEndpoint:
    """``GET /api/audit-log`` is what the MCP tool wraps."""

    async def test_admin_can_list(self, client, db_session):
        await _seed_events(db_session, count=3)

        resp = await client.get("/api/audit-log")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) >= 3
        # Most-recent first
        ts = [e["created_at"] for e in data]
        assert ts == sorted(ts, reverse=True)
        # Required fields for the MCP tool response shape
        for field in ("id", "action", "resource_type", "description", "created_at"):
            assert field in data[0]

    async def test_filter_by_action(self, client, db_session):
        await _seed_events(db_session, count=2, action="asset.delete")
        await _seed_events(db_session, count=2, action="schedule.create",
                           resource_type="schedule")

        resp = await client.get("/api/audit-log", params={"action": "asset.delete"})
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert all(e["action"] == "asset.delete" for e in data)

    async def test_filter_by_resource_type(self, client, db_session):
        await _seed_events(db_session, count=2, action="asset.delete",
                           resource_type="asset")
        await _seed_events(db_session, count=3, action="device.reboot",
                           resource_type="device")

        resp = await client.get("/api/audit-log", params={"resource_type": "device"})
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 3
        assert all(e["resource_type"] == "device" for e in data)

    async def test_filter_by_time_window(self, client, db_session):
        # Seed an old event well before the cutoff
        old = datetime.now(timezone.utc) - timedelta(days=30)
        old_entry = AuditLog(
            action="very.old", resource_type="asset",
            description="before the cutoff", created_at=old,
        )
        # And a fresh event clearly after the cutoff
        new_entry = AuditLog(
            action="new.thing", resource_type="asset",
            description="after the cutoff",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add_all([old_entry, new_entry])
        await db_session.commit()

        cutoff = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        resp = await client.get("/api/audit-log", params={"since": cutoff})
        assert resp.status_code == 200
        data = resp.json()
        actions = {e["action"] for e in data}
        assert "very.old" not in actions
        assert "new.thing" in actions

    async def test_free_text_search(self, client, db_session):
        # description: "deleted asset #0", "deleted asset #1", ...
        await _seed_events(db_session, count=3)
        # Add a distinct entry
        entry = AuditLog(
            action="group.create", resource_type="group",
            description="created special marketing group",
        )
        db_session.add(entry)
        await db_session.commit()

        resp = await client.get("/api/audit-log", params={"q": "marketing"})
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["description"] == "created special marketing group"

    async def test_limit_and_offset(self, client, db_session):
        await _seed_events(db_session, count=5)

        first = await client.get("/api/audit-log", params={"limit": 2, "offset": 0})
        second = await client.get("/api/audit-log", params={"limit": 2, "offset": 2})

        assert first.status_code == 200
        assert second.status_code == 200
        first_data = first.json()
        second_data = second.json()
        assert len(first_data) == 2
        assert len(second_data) == 2
        # Pages don't overlap
        assert {e["id"] for e in first_data} & {e["id"] for e in second_data} == set()

    async def test_limit_clamped_to_500(self, client, db_session):
        # Server clamps limit via Query(..., le=500); over-large limits 422.
        resp = await client.get("/api/audit-log", params={"limit": 5000})
        assert resp.status_code == 422

    async def test_unauthenticated_denied(self, unauthed_client, db_session):
        await _seed_events(db_session, count=1)
        resp = await unauthed_client.get("/api/audit-log")
        # Either a 401 (API-style) or a 302 redirect to login (UI-style)
        assert resp.status_code in (401, 302, 307)


# ---------------------------------------------------------------------------
# CMSClient tests — the MCP tool's client-side contract
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
def cms_client_module(monkeypatch):
    """Import mcp/cms_client.py as a top-level module without triggering
    the ``mcp`` package-vs-pip-package name collision.
    """
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "mcp" / "cms_client.py"
    spec = importlib.util.spec_from_file_location("agora_mcp_cms_client", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.asyncio
class TestCMSClientAuditLog:
    """CMSClient.list_audit_events wire format."""

    async def _client_with_mock(self, cms_client_module, handler):
        transport = httpx.MockTransport(handler)
        c = cms_client_module.CMSClient(base_url="http://cms:8080", api_key="k")
        # Replace the real httpx client with one that uses our mock transport
        await c._client.aclose()
        c._client = httpx.AsyncClient(
            base_url="http://cms:8080",
            transport=transport,
            headers={"X-API-Key": "k"},
        )
        return c

    async def test_no_params_hits_endpoint_with_defaults(self, cms_client_module):
        captured = {}

        def handler(req: httpx.Request) -> httpx.Response:
            captured["url"] = str(req.url)
            captured["params"] = dict(req.url.params)
            return httpx.Response(200, json=[])

        c = await self._client_with_mock(cms_client_module, handler)
        try:
            result = await c.list_audit_events()
        finally:
            await c.close()

        assert result == []
        assert captured["url"].startswith("http://cms:8080/api/audit-log")
        assert captured["params"] == {"limit": "50", "offset": "0"}

    async def test_all_params_passthrough(self, cms_client_module):
        captured = {}

        def handler(req: httpx.Request) -> httpx.Response:
            captured["params"] = dict(req.url.params)
            return httpx.Response(200, json=[{"id": "x"}])

        c = await self._client_with_mock(cms_client_module, handler)
        try:
            result = await c.list_audit_events(
                limit=25,
                offset=10,
                action="asset.delete",
                resource_type="asset",
                user_id="0199a1b0-0000-0000-0000-000000000001",
                since="2026-04-01T00:00:00Z",
                until="2026-04-19T23:59:59Z",
                q="marketing",
            )
        finally:
            await c.close()

        assert result == [{"id": "x"}]
        assert captured["params"] == {
            "limit": "25",
            "offset": "10",
            "action": "asset.delete",
            "resource_type": "asset",
            "user_id": "0199a1b0-0000-0000-0000-000000000001",
            "since": "2026-04-01T00:00:00Z",
            "until": "2026-04-19T23:59:59Z",
            "q": "marketing",
        }

    async def test_none_params_are_omitted(self, cms_client_module):
        captured = {}

        def handler(req: httpx.Request) -> httpx.Response:
            captured["params"] = dict(req.url.params)
            return httpx.Response(200, json=[])

        c = await self._client_with_mock(cms_client_module, handler)
        try:
            await c.list_audit_events(action="schedule.create")  # only action set
        finally:
            await c.close()

        # action must appear; user_id/since/until/q/resource_type must NOT
        assert captured["params"].get("action") == "schedule.create"
        for absent in ("user_id", "since", "until", "q", "resource_type"):
            assert absent not in captured["params"]

    async def test_http_error_propagates(self, cms_client_module):
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(401)

        c = await self._client_with_mock(cms_client_module, handler)
        try:
            with pytest.raises(httpx.HTTPStatusError):
                await c.list_audit_events()
        finally:
            await c.close()
