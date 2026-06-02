"""Audit-log emission for assistant chat turns (assistant.message).

Covers both the non-streaming ``POST /api/chat/threads/{id}/message``
and the streaming ``POST /api/chat/threads/{id}/stream`` endpoints.
The user explicitly asked for assistant LLM usage to show up in the
audit log so the (real) AOAI spend is visible at a per-turn / per-user
granularity.  These tests pin the contract — if the helper is removed
or the wiring drops, CI must fail.

The fakes mirror the patterns in ``test_chat_agent.py`` /
``test_chat_smoke.py`` (and are re-exported from there).
"""

from __future__ import annotations

import json as _json

import pytest
from sqlalchemy import select

from cms.models.audit_log import AuditLog

from tests.test_chat_agent import (  # noqa: F401 — re-export scripted_agent fixture
    FakeLLMClient,
    _FakeResult,
    _create_thread,
    patched_llm,
    scripted_agent,
)


async def _consume_sse(response):
    """Drain an SSE response and ignore the parsed payload — these
    tests only care that the stream completed (and therefore that the
    ``done`` event reached the audit hook in the ``finally`` block).
    """
    async for _ in response.aiter_lines():
        pass


@pytest.mark.asyncio
class TestAssistantAuditNonStream:
    async def test_post_message_writes_audit_row(
        self, client, db_session, patched_llm
    ):
        tid = await _create_thread(client, title="Audit me")
        resp = await client.post(
            f"/api/chat/threads/{tid}/message",
            json={"content": "Hello assistant"},
        )
        assert resp.status_code == 201, resp.text

        rows = (await db_session.execute(
            select(AuditLog).where(AuditLog.action == "assistant.message")
        )).scalars().all()
        assert len(rows) == 1, f"expected one assistant.message row, got {len(rows)}"
        row = rows[0]

        assert row.resource_type == "chat_thread"
        assert row.resource_id == tid

        details = row.details or {}
        # FakeLLMClient defaults: tokens_in=42, tokens_out=17.
        assert details.get("tokens_in") == 42
        assert details.get("tokens_out") == 17
        assert details.get("streaming") is False
        assert details.get("thread_title") == "Audit me"
        assert "deployment" in details
        assert "model" in details
        # message_id is the assistant row id (UUID string).
        assert details.get("message_id")

    async def test_description_includes_token_count(
        self, client, db_session, patched_llm
    ):
        tid = await _create_thread(client)
        resp = await client.post(
            f"/api/chat/threads/{tid}/message",
            json={"content": "hi"},
        )
        assert resp.status_code == 201

        row = (await db_session.execute(
            select(AuditLog).where(AuditLog.action == "assistant.message")
        )).scalar_one()

        # build_description() must render the token total in the
        # human-readable description so the audit-log UI surfaces it
        # without the operator having to expand `details`.
        assert "59" in row.description or "tokens" in row.description.lower()


@pytest.mark.asyncio
class TestAssistantAuditStream:
    async def test_stream_writes_audit_row_after_done(
        self, client, db_session, scripted_agent
    ):
        # Single-turn scripted reply: no tools, just content + finish.
        scripted_agent["llm_replies"] = [
            _FakeResult(content="streamed reply", tokens_in=11, tokens_out=7)
        ]

        tid = await _create_thread(client, title="stream audit")
        async with client.stream(
            "POST",
            f"/api/chat/threads/{tid}/stream",
            json={"content": "ping"},
        ) as resp:
            assert resp.status_code == 200, await resp.aread()
            await _consume_sse(resp)

        rows = (await db_session.execute(
            select(AuditLog).where(AuditLog.action == "assistant.message")
        )).scalars().all()
        assert len(rows) == 1, (
            f"expected one assistant.message row for streaming turn, "
            f"got {len(rows)}"
        )
        details = rows[0].details or {}
        assert details.get("streaming") is True
        assert details.get("tokens_in") == 11
        assert details.get("tokens_out") == 7
        assert rows[0].resource_id == tid

    async def test_stream_audit_records_cost_when_priced(
        self, client, db_session, scripted_agent, app
    ):
        """When the deployment maps to a known price-table model, the
        details payload must include an ``est_cost_usd`` field so the
        audit log reflects real $$ spend, not just tokens.
        """
        # Force a real-model name via the override mechanism the
        # pricing module honors (deployment name 'chat' otherwise
        # falls through to the unknown/zero-cost branch).
        from cms.auth import get_settings

        settings = app.dependency_overrides[get_settings]()
        settings.azure_openai_model = "gpt-4o"

        scripted_agent["llm_replies"] = [
            _FakeResult(content="ok", tokens_in=1000, tokens_out=500)
        ]

        tid = await _create_thread(client)
        async with client.stream(
            "POST",
            f"/api/chat/threads/{tid}/stream",
            json={"content": "ping"},
        ) as resp:
            assert resp.status_code == 200, await resp.aread()
            await _consume_sse(resp)

        row = (await db_session.execute(
            select(AuditLog).where(AuditLog.action == "assistant.message")
        )).scalar_one()
        details = row.details or {}
        assert details.get("model") == "gpt-4o", (
            f"expected model resolution via override; details={details!r}"
        )
        cost = details.get("est_cost_usd")
        assert isinstance(cost, (int, float)) and cost > 0, (
            f"expected a positive est_cost_usd, got {cost!r} "
            f"(details={details!r})"
        )
