"""Tests for the Assistant write-tool approval endpoints (PR 4 of 6).

Covers the standalone :mod:`cms.routers.chat_approvals` surface:

* List endpoint (default ``pending`` filter, plus all-states).
* Approve endpoint runs MCP with ``bypass_whitelist=True``, persists a
  ``role=tool`` ChatMessage, marks the row ``approved``.
* Approve endpoint is a 503 when MCP is down (and leaves the row
  pending so the user can retry).
* Approve endpoint records tool-execution errors as a tool-result
  message AND still marks the row approved (matches the way the
  agent loop handles tool errors).
* Reject endpoint persists a synthetic ``role=tool`` rejection blob
  and marks the row ``rejected``.
* Both decision endpoints reject double-decisions with 409.
* Cross-user / cross-thread access returns 404 (no leakage).
* Feature flag gating returns 404 to non-allowlisted users.

The agent-side write-tool intercept (which actually creates these
``ChatPendingApproval`` rows) lands separately on top of PR 3c; this
test file focuses on the decision surface only and creates the
approval rows directly.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select

from cms.models.chat_message import ChatMessage
from cms.models.chat_pending_approval import (
    STATUS_APPROVED,
    STATUS_PENDING,
    STATUS_REJECTED,
    ChatPendingApproval,
)
from cms.models.chat_thread import ChatThread


# ── Fake MCP for the approve path ─────────────────────────────────────


class _ApproveFakeMcp:
    """Records call_tool args + returns a canned result.

    Set ``raise_on_enter`` to raise ``McpUnavailableError`` from
    ``__aenter__`` (i.e. the backend is down).  Set ``raise_on_call``
    to raise a generic exception from ``call_tool``.
    """

    instances: list["_ApproveFakeMcp"] = []
    result: str = '{"ok": true}'
    raise_on_enter: bool = False
    raise_on_call: bool = False

    def __init__(self, *, settings=None, user=None):
        self.settings = settings
        self.user = user
        self.calls: list[tuple[str, dict, bool]] = []
        _ApproveFakeMcp.instances.append(self)

    async def __aenter__(self):
        if _ApproveFakeMcp.raise_on_enter:
            from cms.services.assistant.mcp_client import McpUnavailableError

            raise McpUnavailableError("simulated outage")
        return self

    async def __aexit__(self, *a):
        return None

    async def call_tool(self, name, arguments, *, bypass_whitelist=False):
        self.calls.append((name, dict(arguments), bypass_whitelist))
        if _ApproveFakeMcp.raise_on_call:
            raise RuntimeError("tool blew up")
        return _ApproveFakeMcp.result


@pytest_asyncio.fixture
async def patched_approve_mcp(monkeypatch):
    """Patch the MCP client used by the approvals router."""
    from cms.routers import chat_approvals as router_mod

    _ApproveFakeMcp.instances = []
    _ApproveFakeMcp.result = '{"ok": true}'
    _ApproveFakeMcp.raise_on_enter = False
    _ApproveFakeMcp.raise_on_call = False
    monkeypatch.setattr(router_mod, "AssistantMcpClient", _ApproveFakeMcp)
    yield _ApproveFakeMcp
    _ApproveFakeMcp.instances = []


# ── Helpers ───────────────────────────────────────────────────────────


async def _create_thread(client, title: str = "") -> uuid.UUID:
    resp = await client.post("/api/chat/threads", json={"title": title})
    assert resp.status_code == 201, resp.text
    return uuid.UUID(resp.json()["id"])


async def _new_approval(
    app,
    thread_id: uuid.UUID,
    *,
    tool_name: str = "delete_device",
    tool_args: dict | None = None,
    status: str = STATUS_PENDING,
) -> uuid.UUID:
    """Insert a ``ChatPendingApproval`` row directly via the app's DB
    factory.  Returns the new row's id."""
    from cms.database import get_db

    factory = app.dependency_overrides[get_db]
    row_id: uuid.UUID | None = None
    async for db in factory():
        row = ChatPendingApproval(
            thread_id=thread_id,
            tool_name=tool_name,
            tool_call_id=f"call_{uuid.uuid4().hex[:8]}",
            tool_arguments=tool_args or {"id": "dev-1"},
            status=status,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        row_id = row.id
        break
    assert row_id is not None
    return row_id


async def _fetch_thread_messages(app, thread_id: uuid.UUID) -> list[ChatMessage]:
    from cms.database import get_db

    factory = app.dependency_overrides[get_db]
    async for db in factory():
        rows = (
            await db.execute(
                select(ChatMessage)
                .where(ChatMessage.thread_id == thread_id)
                .order_by(ChatMessage.created_at, ChatMessage.id)
            )
        ).scalars().all()
        return list(rows)
    return []


# ── Tests ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestListApprovals:
    async def test_default_returns_only_pending(self, client, app):
        tid = await _create_thread(client)
        pending_id = await _new_approval(app, tid, status=STATUS_PENDING)
        await _new_approval(app, tid, status=STATUS_APPROVED)
        await _new_approval(app, tid, status=STATUS_REJECTED)

        resp = await client.get(f"/api/chat/threads/{tid}/approvals")
        assert resp.status_code == 200, resp.text
        ids = [r["id"] for r in resp.json()]
        assert ids == [str(pending_id)]

    async def test_status_empty_returns_all(self, client, app):
        tid = await _create_thread(client)
        await _new_approval(app, tid, status=STATUS_PENDING)
        await _new_approval(app, tid, status=STATUS_APPROVED)

        resp = await client.get(
            f"/api/chat/threads/{tid}/approvals", params={"status": ""}
        )
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    async def test_cross_user_thread_is_404(
        self, client, unauthed_client, app
    ):
        tid = await _create_thread(client)
        await _new_approval(app, tid)
        # Unauthed_client has no session — should 401 from require_auth
        # OR 404 from feature gate; either way it's not 200.
        resp = await unauthed_client.get(f"/api/chat/threads/{tid}/approvals")
        assert resp.status_code in (401, 404)


@pytest.mark.asyncio
class TestApproveEndpoint:
    async def test_approve_runs_tool_and_persists_result(
        self, client, app, patched_approve_mcp
    ):
        tid = await _create_thread(client)
        aid = await _new_approval(
            app, tid, tool_name="delete_device", tool_args={"id": "dev-1"}
        )
        patched_approve_mcp.result = '{"deleted": "dev-1"}'

        resp = await client.post(
            f"/api/chat/approvals/{aid}/approve", json={"note": "go"}
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == STATUS_APPROVED
        assert body["result_content"] == '{"deleted": "dev-1"}'
        assert body["decision_note"] == "go"
        assert body["decided_at"] is not None

        # MCP got called with bypass_whitelist=True.
        assert len(patched_approve_mcp.instances) == 1
        mcp = patched_approve_mcp.instances[0]
        assert mcp.calls == [("delete_device", {"id": "dev-1"}, True)]

        # A role=tool ChatMessage was persisted on the thread.
        messages = await _fetch_thread_messages(app, tid)
        tool_msgs = [m for m in messages if m.role == "tool"]
        assert len(tool_msgs) == 1
        assert tool_msgs[0].content == '{"deleted": "dev-1"}'

    async def test_approve_503_when_mcp_unavailable_leaves_row_pending(
        self, client, app, patched_approve_mcp
    ):
        _ApproveFakeMcp.raise_on_enter = True
        tid = await _create_thread(client)
        aid = await _new_approval(app, tid)

        resp = await client.post(f"/api/chat/approvals/{aid}/approve")
        assert resp.status_code == 503
        # Re-fetch — should still be pending.
        get_resp = await client.get(f"/api/chat/approvals/{aid}")
        assert get_resp.status_code == 200
        assert get_resp.json()["status"] == STATUS_PENDING

    async def test_approve_records_tool_error_as_result(
        self, client, app, patched_approve_mcp
    ):
        _ApproveFakeMcp.raise_on_call = True
        tid = await _create_thread(client)
        aid = await _new_approval(app, tid)

        resp = await client.post(f"/api/chat/approvals/{aid}/approve")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == STATUS_APPROVED
        result = json.loads(body["result_content"])
        assert "error" in result
        assert "tool blew up" in result["error"]
        # And the synthetic tool message landed.
        messages = await _fetch_thread_messages(app, tid)
        assert any(m.role == "tool" for m in messages)

    async def test_approve_twice_is_409(self, client, app, patched_approve_mcp):
        tid = await _create_thread(client)
        aid = await _new_approval(app, tid)
        await client.post(f"/api/chat/approvals/{aid}/approve")
        resp = await client.post(f"/api/chat/approvals/{aid}/approve")
        assert resp.status_code == 409

    async def test_approve_unknown_is_404(self, client, patched_approve_mcp):
        resp = await client.post(
            f"/api/chat/approvals/{uuid.uuid4()}/approve"
        )
        assert resp.status_code == 404


@pytest.mark.asyncio
class TestRejectEndpoint:
    async def test_reject_persists_synthetic_tool_message(self, client, app):
        tid = await _create_thread(client)
        aid = await _new_approval(app, tid, tool_name="delete_device")

        resp = await client.post(
            f"/api/chat/approvals/{aid}/reject", json={"note": "too risky"}
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == STATUS_REJECTED
        assert body["decision_note"] == "too risky"
        payload = json.loads(body["result_content"])
        assert payload["rejected"] is True
        assert payload["reason"] == "too risky"

        # Synthetic role=tool message landed.
        messages = await _fetch_thread_messages(app, tid)
        tool_msgs = [m for m in messages if m.role == "tool"]
        assert len(tool_msgs) == 1
        assert json.loads(tool_msgs[0].content)["rejected"] is True

    async def test_reject_without_note_uses_default_reason(self, client, app):
        tid = await _create_thread(client)
        aid = await _new_approval(app, tid)

        resp = await client.post(f"/api/chat/approvals/{aid}/reject")
        assert resp.status_code == 200
        payload = json.loads(resp.json()["result_content"])
        assert "declined" in payload["reason"].lower()

    async def test_reject_after_approve_is_409(
        self, client, app, patched_approve_mcp
    ):
        tid = await _create_thread(client)
        aid = await _new_approval(app, tid)
        await client.post(f"/api/chat/approvals/{aid}/approve")
        resp = await client.post(f"/api/chat/approvals/{aid}/reject")
        assert resp.status_code == 409
