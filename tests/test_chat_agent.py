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
    tool_calls: list[dict] | None = None


class FakeLLMClient:
    """Stand-in for :class:`cms.services.assistant.llm_client.LLMClient`.

    Records the messages it was called with so tests can assert against
    the prompt construction without mocking the OpenAI SDK.

    In PR 3b a single instance can also be scripted with a sequence of
    canned responses (``replies=[...]``) to exercise the tool-calling
    loop without an LLM.  Each call pops the next response; once the
    sequence is exhausted further calls return the trailing ``reply``
    string with no tool_calls.
    """

    last_instance: "FakeLLMClient | None" = None

    def __init__(
        self,
        settings=None,
        *,
        reply: str = "stub reply",
        replies: list[_FakeResult] | None = None,
    ):
        self.calls: list[list[dict]] = []
        self.tools_seen: list[list[dict] | None] = []
        self.reply = reply
        self._scripted: list[_FakeResult] = list(replies or [])
        self.closed = False
        FakeLLMClient.last_instance = self

    async def complete(
        self,
        messages,
        *,
        max_completion_tokens=None,
        temperature: float = 0.2,
        tools=None,
        tool_choice=None,
    ):
        self.calls.append(list(messages))
        self.tools_seen.append(list(tools) if tools else None)
        if self._scripted:
            return self._scripted.pop(0)
        return _FakeResult(content=self.reply, tokens_in=42, tokens_out=17)

    async def stream(
        self,
        messages,
        *,
        max_completion_tokens=None,
        temperature: float = 0.2,
        tools=None,
        tool_choice=None,
    ):
        """Adapt a scripted ``_FakeResult`` into the streaming delta
        shape ``LLMClient.stream`` yields.  One call = one
        ``_FakeResult`` worth of deltas (one content chunk if the
        reply is text, one tool_call_delta per tool call if any,
        then a ``finish``)."""
        self.calls.append(list(messages))
        self.tools_seen.append(list(tools) if tools else None)
        result = (
            self._scripted.pop(0)
            if self._scripted
            else _FakeResult(content=self.reply, tokens_in=42, tokens_out=17)
        )
        if result.content:
            yield {"type": "content", "text": result.content}
        if result.tool_calls:
            for idx, tc in enumerate(result.tool_calls):
                fn = tc.get("function", {})
                yield {
                    "type": "tool_call_delta",
                    "index": idx,
                    "id": tc.get("id"),
                    "name": fn.get("name"),
                    "arguments_delta": fn.get("arguments", ""),
                }
        yield {
            "type": "finish",
            "reason": "tool_calls" if result.tool_calls else "stop",
            "tokens_in": result.tokens_in,
            "tokens_out": result.tokens_out,
        }

    async def aclose(self) -> None:
        self.closed = True


class FakeMcpClient:
    """In-memory MCP stand-in used by the agent loop tests.

    Returns a fixed (small) tool list from ``list_openai_tools`` and a
    queue of canned results from ``call_tool``.  Records every call for
    assertions.
    """

    last_instance: "FakeMcpClient | None" = None

    def __init__(
        self,
        *,
        settings=None,
        user=None,
        tools: list[dict] | None = None,
        results: dict[str, str] | None = None,
    ):
        self._tools = tools if tools is not None else []
        self._results = dict(results or {})
        self.calls: list[tuple[str, dict]] = []
        self.opened = False
        self.closed = False
        FakeMcpClient.last_instance = self

    async def __aenter__(self) -> "FakeMcpClient":
        self.opened = True
        return self

    async def __aexit__(self, *a) -> None:
        self.closed = True

    async def list_openai_tools(self) -> list[dict]:
        return list(self._tools)

    async def call_tool(self, name: str, arguments: dict) -> str:
        self.calls.append((name, dict(arguments)))
        if name in self._results:
            return self._results[name]
        return f'{{"ok": true, "tool": "{name}"}}'


@pytest_asyncio.fixture
async def patched_llm(app, monkeypatch):
    """Force Azure OpenAI to look configured AND patch the LLMClient class.

    Tests using this fixture get a single shared FakeLLMClient instance
    that can be inspected via ``FakeLLMClient.last_instance``.  The MCP
    client is also patched with a no-tool ``FakeMcpClient`` so tests
    that don't care about the tool loop see the same single-shot
    behaviour they had in PR 3a.
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
    monkeypatch.setattr(agent_mod, "AssistantMcpClient", FakeMcpClient)

    FakeLLMClient.last_instance = None
    FakeMcpClient.last_instance = None
    yield
    FakeLLMClient.last_instance = None
    FakeMcpClient.last_instance = None


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


# ── PR 3b: MCP tool-calling loop ──────────────────────────────────────


def _tool_call(name: str, arguments: dict, call_id: str = "call_1") -> dict:
    """Build an OpenAI-format tool_call dict (mirrors what the SDK
    serialises from a real Azure response)."""
    import json as _json

    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": _json.dumps(arguments)},
    }


@pytest_asyncio.fixture
async def scripted_agent(app, monkeypatch):
    """Patch the agent with a FakeLLMClient + FakeMcpClient pair the
    test can pre-script before posting a message.

    Differs from ``patched_llm`` in that it returns a builder the test
    uses to set the scripted ``replies`` and ``results`` _before_ the
    fakes are constructed (the agent constructs them per turn)."""
    from cms.auth import get_settings
    from cms.routers import chat as chat_router
    from cms.services.assistant import agent as agent_mod

    settings = app.dependency_overrides[get_settings]()
    settings.azure_openai_endpoint = "https://fake.openai.azure.com"
    settings.azure_openai_deployment = "gpt-4o-fake"
    monkeypatch.setattr(chat_router, "assistant_llm_available", lambda s: True)

    state = {
        "llm_replies": [],
        "llm_reply": "stub reply",
        "mcp_tools": [],
        "mcp_results": {},
    }

    class _ScriptedLLM(FakeLLMClient):
        def __init__(self, settings=None):
            super().__init__(
                settings,
                reply=state["llm_reply"],
                replies=list(state["llm_replies"]),
            )

    class _ScriptedMcp(FakeMcpClient):
        def __init__(self, *, settings=None, user=None):
            super().__init__(
                settings=settings,
                user=user,
                tools=list(state["mcp_tools"]),
                results=dict(state["mcp_results"]),
            )

    monkeypatch.setattr(agent_mod, "LLMClient", _ScriptedLLM)
    monkeypatch.setattr(agent_mod, "AssistantMcpClient", _ScriptedMcp)
    FakeLLMClient.last_instance = None
    FakeMcpClient.last_instance = None
    yield state
    FakeLLMClient.last_instance = None
    FakeMcpClient.last_instance = None


@pytest.mark.asyncio
class TestToolLoop:
    async def test_tool_definitions_passed_to_llm(self, client, scripted_agent):
        scripted_agent["mcp_tools"] = [
            {
                "type": "function",
                "function": {
                    "name": "list_devices",
                    "description": "list devices",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]
        tid = (await client.post("/api/chat/threads", json={"title": ""})).json()["id"]
        resp = await client.post(
            f"/api/chat/threads/{tid}/message",
            json={"content": "list devices"},
        )
        assert resp.status_code == 201, resp.text
        # The first LLM call should have received the tool list.
        first_tools = FakeLLMClient.last_instance.tools_seen[0]
        assert first_tools is not None
        assert first_tools[0]["function"]["name"] == "list_devices"

    async def test_single_tool_call_then_final_answer(
        self, client, scripted_agent, app
    ):
        from cms.database import get_db
        from cms.models.chat_message import ChatMessage

        scripted_agent["mcp_tools"] = [
            {
                "type": "function",
                "function": {
                    "name": "list_devices",
                    "description": "list devices",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]
        scripted_agent["mcp_results"] = {"list_devices": '[{"id":"d1"}]'}
        scripted_agent["llm_replies"] = [
            # Turn 1: request the tool.
            _FakeResult(
                content="",
                tokens_in=10,
                tokens_out=5,
                tool_calls=[_tool_call("list_devices", {})],
            ),
            # Turn 2: produce final answer using the tool result.
            _FakeResult(content="You have 1 device.", tokens_in=20, tokens_out=8),
        ]

        tid = (await client.post("/api/chat/threads", json={"title": ""})).json()["id"]
        resp = await client.post(
            f"/api/chat/threads/{tid}/message",
            json={"content": "how many devices"},
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["role"] == "assistant"
        assert body["content"] == "You have 1 device."

        # MCP got the call.
        assert FakeMcpClient.last_instance.calls == [("list_devices", {})]

        # Persisted rows: user → assistant(tool_calls) → tool → assistant(final).
        factory = app.dependency_overrides[get_db]
        async for db in factory():
            rows = (
                await db.execute(
                    select(ChatMessage)
                    .where(ChatMessage.thread_id == uuid.UUID(tid))
                    .order_by(ChatMessage.created_at, ChatMessage.id)
                )
            ).scalars().all()
            assert [r.role for r in rows] == [
                "user",
                "assistant",
                "tool",
                "assistant",
            ]
            assert rows[1].tool_calls is not None
            assert rows[1].tool_calls[0]["function"]["name"] == "list_devices"
            assert rows[2].tool_call_id == "call_1"
            assert rows[2].content == '[{"id":"d1"}]'
            assert rows[3].content == "You have 1 device."
            break

    async def test_history_with_tool_turns_replays_on_second_turn(
        self, client, scripted_agent
    ):
        # Turn 1: one tool call, then final answer.
        scripted_agent["mcp_tools"] = [
            {
                "type": "function",
                "function": {
                    "name": "get_server_time",
                    "description": "time",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]
        scripted_agent["llm_replies"] = [
            _FakeResult(
                content="",
                tokens_in=1,
                tokens_out=1,
                tool_calls=[_tool_call("get_server_time", {})],
            ),
            _FakeResult(content="It is now.", tokens_in=1, tokens_out=1),
        ]
        tid = (await client.post("/api/chat/threads", json={"title": ""})).json()["id"]
        await client.post(
            f"/api/chat/threads/{tid}/message", json={"content": "first"}
        )

        # Turn 2: scripted_agent rebuilds the LLM, no scripted replies →
        # stub reply, no tool calls.  The point is the messages= list
        # passed into the new LLM call must include the prior tool turn.
        scripted_agent["llm_replies"] = []
        scripted_agent["llm_reply"] = "second"
        await client.post(
            f"/api/chat/threads/{tid}/message", json={"content": "second"}
        )

        messages = FakeLLMClient.last_instance.calls[0]
        roles = [m["role"] for m in messages]
        # system + user(first) + assistant(tool_calls) + tool + assistant(final) + user(second)
        assert roles == [
            "system",
            "user",
            "assistant",
            "tool",
            "assistant",
            "user",
        ]

    async def test_write_tool_blocked_via_whitelist(
        self, client, scripted_agent, app
    ):
        from cms.database import get_db
        from cms.models.chat_message import ChatMessage

        # MCP exposes only the read tool, but the LLM hallucinates a
        # write call name.  The agent must refuse, persist a synthetic
        # error tool result, and let the LLM continue.
        scripted_agent["mcp_tools"] = [
            {
                "type": "function",
                "function": {
                    "name": "get_server_time",
                    "description": "time",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]
        scripted_agent["llm_replies"] = [
            _FakeResult(
                content="",
                tokens_in=1,
                tokens_out=1,
                tool_calls=[_tool_call("delete_device", {"id": "x"})],
            ),
            _FakeResult(content="I can't do that.", tokens_in=1, tokens_out=1),
        ]
        tid = (await client.post("/api/chat/threads", json={"title": ""})).json()["id"]
        resp = await client.post(
            f"/api/chat/threads/{tid}/message",
            json={"content": "delete dev x"},
        )
        assert resp.status_code == 201
        # MCP should NOT have been called for the write.
        assert FakeMcpClient.last_instance.calls == []

        factory = app.dependency_overrides[get_db]
        async for db in factory():
            rows = (
                await db.execute(
                    select(ChatMessage)
                    .where(ChatMessage.thread_id == uuid.UUID(tid))
                    .order_by(ChatMessage.created_at, ChatMessage.id)
                )
            ).scalars().all()
            tool_row = next(r for r in rows if r.role == "tool")
            assert "tool_not_allowed" in tool_row.content
            break

    async def test_invalid_json_arguments_handled(self, client, scripted_agent):
        scripted_agent["mcp_tools"] = [
            {
                "type": "function",
                "function": {
                    "name": "list_devices",
                    "description": "",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]
        bad_call = {
            "id": "call_x",
            "type": "function",
            "function": {"name": "list_devices", "arguments": "{not json"},
        }
        scripted_agent["llm_replies"] = [
            _FakeResult(
                content="",
                tokens_in=1,
                tokens_out=1,
                tool_calls=[bad_call],
            ),
            _FakeResult(content="ok", tokens_in=1, tokens_out=1),
        ]
        tid = (await client.post("/api/chat/threads", json={"title": ""})).json()["id"]
        resp = await client.post(
            f"/api/chat/threads/{tid}/message", json={"content": "hi"}
        )
        assert resp.status_code == 201
        assert FakeMcpClient.last_instance.calls == []  # never reached MCP
        # The error gets fed back to the LLM on turn 2.
        second_call_msgs = FakeLLMClient.last_instance.calls[1]
        tool_msg = next(m for m in second_call_msgs if m["role"] == "tool")
        assert "bad_arguments" in tool_msg["content"]

    async def test_max_iterations_returns_safe_message(
        self, client, scripted_agent, app
    ):
        scripted_agent["mcp_tools"] = [
            {
                "type": "function",
                "function": {
                    "name": "get_server_time",
                    "description": "",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]
        # Lower the cap so the test is fast.
        settings = app.dependency_overrides[
            __import__("cms.auth", fromlist=["get_settings"]).get_settings
        ]()
        settings.assistant_max_tool_iterations = 2

        # Always returns a tool call → loop exhausts.
        tc = _tool_call("get_server_time", {})
        scripted_agent["llm_replies"] = [
            _FakeResult(content="", tokens_in=1, tokens_out=1, tool_calls=[tc]),
            _FakeResult(content="", tokens_in=1, tokens_out=1, tool_calls=[tc]),
        ]
        tid = (await client.post("/api/chat/threads", json={"title": ""})).json()["id"]
        resp = await client.post(
            f"/api/chat/threads/{tid}/message", json={"content": "loop forever"}
        )
        assert resp.status_code == 201
        body = resp.json()
        assert "max tool iterations" in body["content"]

    async def test_mcp_unavailable_returns_503(self, client, app, monkeypatch):
        # LLM is configured, but the MCP client raises on enter.
        from cms.auth import get_settings
        from cms.routers import chat as chat_router
        from cms.services.assistant import agent as agent_mod
        from cms.services.assistant.mcp_client import McpUnavailableError

        settings = app.dependency_overrides[get_settings]()
        settings.azure_openai_endpoint = "https://fake.openai.azure.com"
        settings.azure_openai_deployment = "gpt-4o-fake"
        monkeypatch.setattr(
            chat_router, "assistant_llm_available", lambda s: True
        )
        monkeypatch.setattr(agent_mod, "LLMClient", FakeLLMClient)

        class _BrokenMcp:
            def __init__(self, *, settings=None, user=None):
                pass

            async def __aenter__(self):
                raise McpUnavailableError("simulated outage")

            async def __aexit__(self, *a):
                pass

        monkeypatch.setattr(agent_mod, "AssistantMcpClient", _BrokenMcp)

        tid = (await client.post("/api/chat/threads", json={"title": ""})).json()["id"]
        resp = await client.post(
            f"/api/chat/threads/{tid}/message", json={"content": "hi"}
        )
        assert resp.status_code == 503
        assert "tool backend" in resp.json()["detail"].lower()


# ── PR 3b: MCP client unit tests ──────────────────────────────────────


class TestReadOnlyWhitelist:
    def test_known_read_tools_present(self):
        from cms.services.assistant.mcp_client import READ_ONLY_TOOLS

        for name in (
            "list_devices",
            "get_device",
            "list_groups",
            "list_assets",
            "list_schedules",
            "get_server_time",
            "list_audit_events",
        ):
            assert name in READ_ONLY_TOOLS, name

    def test_known_write_tools_excluded(self):
        from cms.services.assistant.mcp_client import READ_ONLY_TOOLS

        for name in (
            "delete_device",
            "adopt_device",
            "reboot_device",
            "create_group",
            "delete_group",
            "create_schedule",
            "delete_schedule",
            "play_now",
            "factory_reset_device",
            "set_device_password",
        ):
            assert name not in READ_ONLY_TOOLS, name


def test_read_service_key_missing_file(tmp_path):
    from cms.config import Settings
    from cms.services.assistant.mcp_client import (
        McpUnavailableError,
        _read_service_key,
    )

    s = Settings(service_key_path=str(tmp_path / "nope.key"))
    with pytest.raises(McpUnavailableError):
        _read_service_key(s)


def test_read_service_key_empty_file(tmp_path):
    from cms.config import Settings
    from cms.services.assistant.mcp_client import (
        McpUnavailableError,
        _read_service_key,
    )

    p = tmp_path / "k"
    p.write_text("   \n")
    s = Settings(service_key_path=str(p))
    with pytest.raises(McpUnavailableError):
        _read_service_key(s)


def test_read_service_key_ok(tmp_path):
    from cms.config import Settings
    from cms.services.assistant.mcp_client import _read_service_key

    p = tmp_path / "k"
    p.write_text("agora_svc_deadbeef\n")
    s = Settings(service_key_path=str(p))
    assert _read_service_key(s) == "agora_svc_deadbeef"


def test_read_service_key_keyvault_fallback(tmp_path, monkeypatch):
    """When the local file is missing but Key Vault is configured,
    fall back to reading the key from KV (the Azure Container Apps
    deployment shape — no shared volume between CMS and MCP)."""
    from cms.config import Settings
    from cms.services.assistant.mcp_client import _read_service_key

    monkeypatch.setattr(
        "cms.keyvault.read_key_from_keyvault",
        lambda uri: "agora_svc_fromkv",
    )
    s = Settings(
        service_key_path=str(tmp_path / "nope.key"),
        azure_keyvault_uri="https://example-kv.vault.azure.net",
    )
    assert _read_service_key(s) == "agora_svc_fromkv"


def test_read_service_key_keyvault_empty_raises(tmp_path, monkeypatch):
    """Both file missing and KV returning empty → McpUnavailableError."""
    from cms.config import Settings
    from cms.services.assistant.mcp_client import (
        McpUnavailableError,
        _read_service_key,
    )

    monkeypatch.setattr(
        "cms.keyvault.read_key_from_keyvault",
        lambda uri: "",
    )
    s = Settings(
        service_key_path=str(tmp_path / "nope.key"),
        azure_keyvault_uri="https://example-kv.vault.azure.net",
    )
    with pytest.raises(McpUnavailableError):
        _read_service_key(s)


# ── PR 3c: SSE streaming endpoint ─────────────────────────────────────


def _parse_sse(text: str) -> list[tuple[str, str]]:
    """Return ``[(event_name, data_json), ...]`` from an SSE blob.

    Only handles the subset the agent emits — a sequence of frames of
    the form ``event: foo\\ndata: {...}\\n\\n``.  Heartbeat ping frames
    sent by ``sse-starlette`` (``: ping\\n\\n``) are skipped.
    """
    frames: list[tuple[str, str]] = []
    # SSE frames use ``\r\n`` line endings; normalise first.
    normalised = text.replace("\r\n", "\n")
    for raw in normalised.split("\n\n"):
        block = raw.strip()
        if not block or block.startswith(":"):
            continue
        event = None
        data = None
        for line in block.splitlines():
            if line.startswith("event:"):
                event = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data = line[len("data:"):].strip()
        if event is not None and data is not None:
            frames.append((event, data))
    return frames


@pytest.mark.asyncio
class TestStreamingEndpoint:
    async def test_text_only_stream_yields_token_and_done(
        self, client, scripted_agent
    ):
        scripted_agent["mcp_tools"] = []
        scripted_agent["llm_replies"] = [
            _FakeResult(content="Hello world", tokens_in=3, tokens_out=2)
        ]
        tid = (await client.post("/api/chat/threads", json={"title": ""})).json()["id"]
        async with client.stream(
            "POST",
            f"/api/chat/threads/{tid}/stream",
            json={"content": "hi"},
        ) as resp:
            assert resp.status_code == 200
            body = await resp.aread()
        text = body.decode()
        frames = _parse_sse(text)
        events = [name for name, _ in frames]
        assert events == ["token", "done"]
        import json as _json

        assert _json.loads(frames[0][1])["text"] == "Hello world"
        done = _json.loads(frames[-1][1])
        assert done["tokens_in"] == 3
        assert done["tokens_out"] == 2
        assert "message_id" in done

    async def test_stream_with_tool_call_then_final_answer(
        self, client, scripted_agent, app
    ):
        from cms.database import get_db
        from cms.models.chat_message import ChatMessage

        scripted_agent["mcp_tools"] = [
            {
                "type": "function",
                "function": {
                    "name": "list_devices",
                    "description": "",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]
        scripted_agent["mcp_results"] = {"list_devices": '[{"id":"d1"}]'}
        scripted_agent["llm_replies"] = [
            _FakeResult(
                content="",
                tokens_in=10,
                tokens_out=5,
                tool_calls=[_tool_call("list_devices", {})],
            ),
            _FakeResult(content="One device.", tokens_in=20, tokens_out=8),
        ]
        tid = (await client.post("/api/chat/threads", json={"title": ""})).json()["id"]
        async with client.stream(
            "POST",
            f"/api/chat/threads/{tid}/stream",
            json={"content": "how many"},
        ) as resp:
            assert resp.status_code == 200
            body = await resp.aread()
        frames = _parse_sse(body.decode())
        events = [name for name, _ in frames]
        # The streaming agent emits tool_call → tool_result → (final
        # text) token(s) → done.  No assistant token frame for the
        # intermediate empty-content tool-call turn.
        assert events == ["tool_call", "tool_result", "token", "done"]

        # Persisted rows: user, assistant(tool_calls), tool, assistant(final).
        factory = app.dependency_overrides[get_db]
        async for db in factory():
            rows = (
                await db.execute(
                    select(ChatMessage)
                    .where(ChatMessage.thread_id == uuid.UUID(tid))
                    .order_by(ChatMessage.created_at, ChatMessage.id)
                )
            ).scalars().all()
            assert [r.role for r in rows] == [
                "user",
                "assistant",
                "tool",
                "assistant",
            ]
            assert rows[3].content == "One device."
            break

    async def test_stream_503_when_llm_not_configured(self, client):
        # No patched_llm fixture — settings have empty endpoint.
        tid = (await client.post("/api/chat/threads", json={"title": ""})).json()["id"]
        async with client.stream(
            "POST",
            f"/api/chat/threads/{tid}/stream",
            json={"content": "hi"},
        ) as resp:
            assert resp.status_code == 503

    async def test_stream_404_on_unknown_thread(self, client, scripted_agent):
        async with client.stream(
            "POST",
            f"/api/chat/threads/{uuid.uuid4()}/stream",
            json={"content": "hi"},
        ) as resp:
            assert resp.status_code == 404

    async def test_stream_emits_error_event_on_mcp_failure(
        self, client, app, monkeypatch
    ):
        from cms.auth import get_settings
        from cms.routers import chat as chat_router
        from cms.services.assistant import agent as agent_mod
        from cms.services.assistant.mcp_client import McpUnavailableError

        settings = app.dependency_overrides[get_settings]()
        settings.azure_openai_endpoint = "https://fake.openai.azure.com"
        settings.azure_openai_deployment = "gpt-4o-fake"
        monkeypatch.setattr(
            chat_router, "assistant_llm_available", lambda s: True
        )
        monkeypatch.setattr(agent_mod, "LLMClient", FakeLLMClient)

        class _BrokenMcp:
            def __init__(self, *, settings=None, user=None):
                pass

            async def __aenter__(self):
                raise McpUnavailableError("simulated outage")

            async def __aexit__(self, *a):
                pass

        monkeypatch.setattr(agent_mod, "AssistantMcpClient", _BrokenMcp)

        tid = (await client.post("/api/chat/threads", json={"title": ""})).json()["id"]
        async with client.stream(
            "POST",
            f"/api/chat/threads/{tid}/stream",
            json={"content": "hi"},
        ) as resp:
            # Handshake succeeds (LLM looks configured); the failure is
            # surfaced as an error frame.
            assert resp.status_code == 200
            body = await resp.aread()
        frames = _parse_sse(body.decode())
        assert any(name == "error" for name, _ in frames)
        err_frame = next(d for name, d in frames if name == "error")
        assert "simulated outage" in err_frame


# ── PR 4: streaming approval intercept ────────────────────────────────


@pytest.mark.asyncio
class TestStreamingApprovalIntercept:
    async def test_write_tool_yields_approval_request_and_stops(
        self, client, scripted_agent, app
    ):
        from cms.database import get_db
        from cms.models.chat_message import ChatMessage
        from cms.models.chat_pending_approval import (
            STATUS_PENDING,
            ChatPendingApproval,
        )

        # ``delete_device`` is intentionally NOT in READ_ONLY_TOOLS.
        scripted_agent["mcp_tools"] = [
            {
                "type": "function",
                "function": {
                    "name": "delete_device",
                    "description": "delete a device",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]
        scripted_agent["llm_replies"] = [
            _FakeResult(
                content="",
                tokens_in=12,
                tokens_out=4,
                tool_calls=[
                    _tool_call("delete_device", {"id": "dev-1"})
                ],
            ),
            # Should NOT be consumed — the loop must stop on the
            # approval intercept and not call the LLM again.
            _FakeResult(content="should not appear", tokens_in=0, tokens_out=0),
        ]

        tid = (await client.post("/api/chat/threads", json={"title": ""})).json()["id"]
        async with client.stream(
            "POST",
            f"/api/chat/threads/{tid}/stream",
            json={"content": "delete it"},
        ) as resp:
            assert resp.status_code == 200
            body = await resp.aread()

        frames = _parse_sse(body.decode())
        events = [name for name, _ in frames]
        # tool_call → approval_request (no tool_result) → done.
        assert events == ["tool_call", "approval_request", "done"]

        import json as _json

        ar = _json.loads(frames[1][1])
        assert ar["name"] == "delete_device"
        assert ar["arguments"] == {"id": "dev-1"}
        assert "approval_id" in ar

        # An approval row was created.
        factory = app.dependency_overrides[get_db]
        async for db in factory():
            rows = (
                await db.execute(
                    select(ChatPendingApproval).where(
                        ChatPendingApproval.thread_id == uuid.UUID(tid)
                    )
                )
            ).scalars().all()
            assert len(rows) == 1
            assert rows[0].tool_name == "delete_device"
            assert rows[0].status == STATUS_PENDING
            assert rows[0].tool_arguments == {"id": "dev-1"}

            # Placeholder ``role=tool`` message persisted so OpenAI
            # history stays consistent on the next turn.
            msgs = (
                await db.execute(
                    select(ChatMessage)
                    .where(ChatMessage.thread_id == uuid.UUID(tid))
                    .order_by(ChatMessage.created_at, ChatMessage.id)
                )
            ).scalars().all()
            roles = [m.role for m in msgs]
            assert roles == ["user", "assistant", "tool", "assistant"]
            placeholder = _json.loads(msgs[2].content)
            assert placeholder["status"] == "awaiting_approval"
            assert placeholder["tool"] == "delete_device"
            # Final assistant message explains the pause.
            assert "awaiting your approval" in msgs[3].content
            break

        # Only one LLM call was made — the loop stopped on the
        # approval intercept and didn't iterate again.
        assert FakeLLMClient.last_instance is not None
        assert len(FakeLLMClient.last_instance.calls) == 1

    async def test_read_tool_alongside_write_intercepts_only_write(
        self, client, scripted_agent, app
    ):
        from cms.database import get_db
        from cms.models.chat_pending_approval import ChatPendingApproval

        scripted_agent["mcp_tools"] = [
            {
                "type": "function",
                "function": {
                    "name": "list_devices",
                    "description": "",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "delete_device",
                    "description": "",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
        ]
        scripted_agent["mcp_results"] = {"list_devices": "[]"}
        scripted_agent["llm_replies"] = [
            _FakeResult(
                content="",
                tokens_in=8,
                tokens_out=3,
                tool_calls=[
                    {
                        "id": "call_read",
                        "type": "function",
                        "function": {
                            "name": "list_devices",
                            "arguments": "{}",
                        },
                    },
                    {
                        "id": "call_write",
                        "type": "function",
                        "function": {
                            "name": "delete_device",
                            "arguments": '{"id": "dev-1"}',
                        },
                    },
                ],
            )
        ]

        tid = (await client.post("/api/chat/threads", json={"title": ""})).json()["id"]
        async with client.stream(
            "POST",
            f"/api/chat/threads/{tid}/stream",
            json={"content": "list then delete"},
        ) as resp:
            assert resp.status_code == 200
            body = await resp.aread()

        frames = _parse_sse(body.decode())
        events = [name for name, _ in frames]
        # Read tool executes normally; write tool becomes approval.
        # Order: tool_call(read) → tool_result(read) → tool_call(write)
        # → approval_request(write) → done.
        assert events == [
            "tool_call",
            "tool_result",
            "tool_call",
            "approval_request",
            "done",
        ]

        factory = app.dependency_overrides[get_db]
        async for db in factory():
            approvals = (
                await db.execute(
                    select(ChatPendingApproval).where(
                        ChatPendingApproval.thread_id == uuid.UUID(tid)
                    )
                )
            ).scalars().all()
            assert len(approvals) == 1
            assert approvals[0].tool_name == "delete_device"
            break




