"""Tests for the Assistant agent loop (PR 3a — non-streaming, no tools).

Covers:
* ``POST /api/chat/threads/{id}/message`` round-trip with a stubbed LLM
  client so we don't depend on Azure OpenAI in CI.
* 503 fallback when the LLM backend isn't configured.
* Persistence of both user and assistant turns, with token accounting.
* Auto-titling of empty threads from the first user prompt.
* ``updated_at`` bump so the thread sorts to the top after a turn.
* Direct unit-level checks against
  :func:`cms.services.assistant.agent.run_user_turn` to lock the
  conversation construction down without going through HTTP.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select


# ── Fakes ─────────────────────────────────────────────────────────────


@dataclass
class _FakeResult:
    content: str
    tokens_in: int
    tokens_out: int


class FakeLLMClient:
    """Stand-in for :class:`cms.services.assistant.llm_client.LLMClient`.

    Records the messages it was called with so tests can assert against
    the prompt construction without mocking the OpenAI SDK.
    """

    last_instance: "FakeLLMClient | None" = None

    def __init__(self, settings=None, *, reply: str = "stub reply"):
        self.calls: list[list[dict]] = []
        self.reply = reply
        self.closed = False
        FakeLLMClient.last_instance = self

    async def complete(self, messages, *, max_completion_tokens=None,
                       temperature: float = 0.2):
        self.calls.append(list(messages))
        return _FakeResult(content=self.reply, tokens_in=42, tokens_out=17)

    async def aclose(self) -> None:
        self.closed = True


@pytest_asyncio.fixture
async def patched_llm(app, monkeypatch):
    """Force Azure OpenAI to look configured AND patch the LLMClient class.

    Tests using this fixture get a single shared FakeLLMClient instance
    that can be inspected via ``FakeLLMClient.last_instance``.
    """
    from cms.auth import get_settings
    from cms.routers import chat as chat_router
    from cms.services.assistant import agent as agent_mod

    settings = app.dependency_overrides[get_settings]()
    settings.azure_openai_endpoint = "https://fake.openai.azure.com"
    settings.azure_openai_deployment = "gpt-4o-fake"

    # Patch the `is_available` symbol the router imported under a
    # different name as well as the module-level reference.
    monkeypatch.setattr(chat_router, "assistant_llm_available", lambda s: True)
    monkeypatch.setattr(agent_mod, "LLMClient", FakeLLMClient)

    FakeLLMClient.last_instance = None
    yield
    FakeLLMClient.last_instance = None


async def _create_thread(client, title: str = "") -> str:
    resp = await client.post("/api/chat/threads", json={"title": title})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


# ── HTTP-level tests ──────────────────────────────────────────────────


@pytest.mark.asyncio
class TestPostMessageHTTP:
    async def test_round_trip_persists_user_and_assistant(
        self, client, patched_llm
    ):
        tid = await _create_thread(client, title="Promo planning")
        resp = await client.post(
            f"/api/chat/threads/{tid}/message",
            json={"content": "Hello"},
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["role"] == "assistant"
        assert body["content"] == "stub reply"
        assert body["thread_id"] == tid

        # Both user and assistant rows now visible in /messages.
        listing = (await client.get(
            f"/api/chat/threads/{tid}/messages"
        )).json()
        assert [m["role"] for m in listing] == ["user", "assistant"]
        assert listing[0]["content"] == "Hello"
        assert listing[1]["content"] == "stub reply"

    async def test_503_when_llm_not_configured(self, client):
        # No patched_llm — settings have empty endpoint/deployment.
        tid = await _create_thread(client)
        resp = await client.post(
            f"/api/chat/threads/{tid}/message",
            json={"content": "Hello"},
        )
        assert resp.status_code == 503

    async def test_cross_user_thread_404s(
        self, client, operator_client, app, patched_llm
    ):
        # Allowlist the operator so its create call succeeds, then have
        # admin try to post a message to the operator's thread.
        from cms.services.assistant_flag import set_allowlist
        from cms.database import get_db

        factory = app.dependency_overrides[get_db]
        async for db in factory():
            await set_allowlist(db, [operator_client.user_id])
            break

        created = await operator_client.post(
            "/api/chat/threads", json={"title": "ops"}
        )
        op_tid = created.json()["id"]
        resp = await client.post(
            f"/api/chat/threads/{op_tid}/message",
            json={"content": "Hello"},
        )
        assert resp.status_code == 404

    async def test_empty_content_rejected(self, client, patched_llm):
        tid = await _create_thread(client)
        resp = await client.post(
            f"/api/chat/threads/{tid}/message",
            json={"content": ""},
        )
        assert resp.status_code == 422

    async def test_unknown_thread_404(self, client, patched_llm):
        resp = await client.post(
            f"/api/chat/threads/{uuid.uuid4()}/message",
            json={"content": "Hello"},
        )
        assert resp.status_code == 404

    async def test_feature_flag_gates_message_endpoint(
        self, operator_client, app, patched_llm
    ):
        # Operator with no allowlist entry should see a 404 on the
        # message endpoint just like the rest of the surface.
        from cms.services.assistant_flag import set_allowlist
        from cms.database import get_db

        factory = app.dependency_overrides[get_db]
        async for db in factory():
            await set_allowlist(db, [])
            break

        resp = await operator_client.post(
            f"/api/chat/threads/{uuid.uuid4()}/message",
            json={"content": "Hello"},
        )
        assert resp.status_code == 404


# ── Service-level tests ───────────────────────────────────────────────


@pytest.mark.asyncio
class TestAgentService:
    async def test_auto_titles_empty_thread_from_first_prompt(
        self, client, patched_llm, app
    ):
        from cms.database import get_db
        from cms.models.chat_thread import ChatThread

        tid = await _create_thread(client, title="")
        await client.post(
            f"/api/chat/threads/{tid}/message",
            json={"content": "Schedule the promo for Saturday"},
        )

        factory = app.dependency_overrides[get_db]
        async for db in factory():
            t = (await db.execute(
                select(ChatThread).where(ChatThread.id == uuid.UUID(tid))
            )).scalar_one()
            assert t.title == "Schedule the promo for Saturday"
            break

    async def test_does_not_retitle_existing_title(
        self, client, patched_llm, app
    ):
        from cms.database import get_db
        from cms.models.chat_thread import ChatThread

        tid = await _create_thread(client, title="My Title")
        await client.post(
            f"/api/chat/threads/{tid}/message",
            json={"content": "different prompt"},
        )

        factory = app.dependency_overrides[get_db]
        async for db in factory():
            t = (await db.execute(
                select(ChatThread).where(ChatThread.id == uuid.UUID(tid))
            )).scalar_one()
            assert t.title == "My Title"
            break

    async def test_token_usage_persisted_on_assistant_turn(
        self, client, patched_llm, app
    ):
        from cms.database import get_db
        from cms.models.chat_message import ChatMessage

        tid = await _create_thread(client)
        await client.post(
            f"/api/chat/threads/{tid}/message",
            json={"content": "Hello"},
        )

        factory = app.dependency_overrides[get_db]
        async for db in factory():
            rows = (await db.execute(
                select(ChatMessage)
                .where(ChatMessage.thread_id == uuid.UUID(tid))
                .order_by(ChatMessage.created_at)
            )).scalars().all()
            assert rows[0].role == "user"
            assert rows[0].tokens_in == 0  # user turns don't accumulate
            assert rows[1].role == "assistant"
            assert rows[1].tokens_in == 42
            assert rows[1].tokens_out == 17
            break

    async def test_system_prompt_included_in_llm_call(
        self, client, patched_llm
    ):
        tid = await _create_thread(client)
        await client.post(
            f"/api/chat/threads/{tid}/message",
            json={"content": "Hello"},
        )
        call = FakeLLMClient.last_instance.calls[0]
        # First message must be the system prompt; subsequent the user
        # turn we just posted.
        assert call[0]["role"] == "system"
        assert "Agora CMS Assistant" in call[0]["content"]
        assert call[-1]["role"] == "user"
        assert call[-1]["content"] == "Hello"

    async def test_history_replayed_on_subsequent_turn(
        self, client, patched_llm
    ):
        tid = await _create_thread(client)
        await client.post(
            f"/api/chat/threads/{tid}/message",
            json={"content": "First"},
        )
        await client.post(
            f"/api/chat/threads/{tid}/message",
            json={"content": "Second"},
        )
        last_call = FakeLLMClient.last_instance.calls[-1]
        roles = [m["role"] for m in last_call]
        # system + user + assistant (from turn 1) + user (turn 2)
        assert roles == ["system", "user", "assistant", "user"]
        assert last_call[1]["content"] == "First"
        assert last_call[2]["content"] == "stub reply"
        assert last_call[3]["content"] == "Second"

    async def test_updated_at_bumps_on_turn(
        self, client, patched_llm, app
    ):
        from cms.database import get_db
        from cms.models.chat_thread import ChatThread

        tid = await _create_thread(client)
        factory = app.dependency_overrides[get_db]
        async for db in factory():
            t1 = (await db.execute(
                select(ChatThread).where(ChatThread.id == uuid.UUID(tid))
            )).scalar_one()
            before = t1.updated_at
            break

        # Sleep a hair so we can detect the bump even on coarse clocks.
        import asyncio
        await asyncio.sleep(0.01)

        await client.post(
            f"/api/chat/threads/{tid}/message",
            json={"content": "trigger"},
        )

        async for db in factory():
            t2 = (await db.execute(
                select(ChatThread).where(ChatThread.id == uuid.UUID(tid))
            )).scalar_one()
            assert t2.updated_at > before
            break


# ── Prompt builder unit tests ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_prompt_includes_user_identity_and_time(app):
    from datetime import datetime, timezone
    from cms.database import get_db
    from cms.models.user import User
    from cms.services.assistant.prompts import build_system_prompt

    factory = app.dependency_overrides[get_db]
    async for db in factory():
        admin = (await db.execute(
            select(User).where(User.username == "admin")
        )).scalar_one()
        prompt = build_system_prompt(
            admin,
            now=datetime(2026, 5, 28, 20, 0, 0, tzinfo=timezone.utc),
        )
        assert "admin" in prompt
        assert "2026-05-28 20:00 UTC" in prompt
        assert "Agora CMS Assistant" in prompt
        break


def test_llm_client_unavailable_raises(monkeypatch):
    from cms.config import Settings
    from cms.services.assistant.llm_client import (
        AssistantUnavailableError,
        LLMClient,
    )

    s = Settings(
        database_url="sqlite:///x",
        secret_key="x",
        azure_openai_endpoint="",
        azure_openai_deployment="",
    )
    with pytest.raises(AssistantUnavailableError):
        LLMClient(s)


def test_llm_client_is_available_helper():
    from cms.config import Settings
    from cms.services.assistant.llm_client import is_available

    s = Settings(database_url="sqlite:///x", secret_key="x")
    assert is_available(s) is False
    s.azure_openai_endpoint = "https://x.openai.azure.com"
    s.azure_openai_deployment = "gpt"
    assert is_available(s) is True
