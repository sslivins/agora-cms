"""End-to-end smoke tests for the Assistant streaming pipeline.

The Assistant feature is wired across four modules that have drifted
in incompatible ways before:

* ``cms.services.assistant.prompts``     — system prompt text
* ``cms.services.assistant.mcp_client``  — read/write tool whitelist
                                            and OpenAI tool-list filter
* ``cms.services.assistant.agent``       — streaming loop + approval
                                            intercept
* ``cms.routers.chat``                   — ``POST /api/chat/threads/{id}/stream``
                                            SSE endpoint

Each module has good unit coverage, but bugs landed in dev because
cross-module assumptions silently drifted out of sync:

* **#667** – the system prompt told the LLM it had *no* tools even
  though tools were being passed.  All unit tests for ``prompts.py``
  passed; the LLM just refused to use the tools.
* **#670** – ``list_openai_tools`` was filtering by ``READ_ONLY_TOOLS``
  instead of ``ALLOWED_TOOLS``, so every write tool was hidden from
  the LLM even after the PR-4 approval flow shipped.  Unit tests for
  the whitelist constants passed; the integration just didn't wire
  them through.

These smoke tests assert the **contracts between the four modules**
end-to-end with realistic-ish scripted dependencies, so a future
refactor breaks a test instead of breaking dev.
"""
from __future__ import annotations

import json
import uuid

import pytest

from tests.test_chat_agent import (  # reuse existing scripted doubles
    FakeLLMClient,
    FakeMcpClient,
    _FakeResult,
    _create_thread,
    scripted_agent,  # noqa: F401 — re-export so pytest finds the fixture
)


# ── #667 regression: system prompt contract ─────────────────────────


class TestSystemPromptContract:
    """The system prompt must advertise the tool capabilities the
    pipeline actually exposes — otherwise the LLM refuses to use them.
    """

    def _user(self):
        from cms.models.user import User

        return User(id=uuid.uuid4(), username="smoke", email="smoke@example.com")

    def test_mentions_read_tools_with_examples(self):
        """Prompt must tell the LLM it can READ from the deployment
        and give at least one example tool name (so the LLM doesn't
        ask the user to "go look in the UI" — the #667 failure mode).
        """
        from cms.services.assistant.prompts import build_system_prompt

        prompt = build_system_prompt(self._user())

        assert "tools" in prompt.lower()
        assert any(
            name in prompt
            for name in ("list_devices", "list_schedules", "list_assets")
        ), "system prompt must name at least one read tool"

    def test_mentions_write_tools_and_approval_flow(self):
        """Prompt must tell the LLM it can WRITE and explain the
        approval contract — otherwise the LLM refuses CRUD requests
        even though the tools are exposed.
        """
        from cms.services.assistant.prompts import build_system_prompt

        prompt = build_system_prompt(self._user()).lower()
        assert "write" in prompt, "prompt must mention write tools"
        assert "approv" in prompt, (
            "prompt must explain the approval flow so the LLM sets "
            "user expectations correctly"
        )

    def test_warns_against_inventing_write_tool_params(self):
        """Prompt must warn the LLM not to guess optional write-tool
        parameters (#672: LLM silently filled in loop_count /
        days_of_week defaults the user never asked for).
        """
        from cms.services.assistant.prompts import build_system_prompt

        prompt = build_system_prompt(self._user()).lower()
        assert any(kw in prompt for kw in ("invent", "guess", "do not silently")), (
            "prompt must warn against inventing write-tool parameters"
        )


# ── #670 regression: MCP tool exposure filter ───────────────────────


class _FakeMcpTool:
    """Mirrors the shape of ``mcp.types.Tool`` enough for
    :py:meth:`AssistantMcpClient.list_openai_tools` to consume.
    """

    def __init__(self, name: str, description: str = "", schema: dict | None = None):
        self.name = name
        self.description = description
        self.inputSchema = schema or {"type": "object", "properties": {}}


class _FakeMcpListing:
    def __init__(self, tools):
        self.tools = tools


class _FakeMcpSession:
    def __init__(self, tools):
        self._tools = tools

    async def list_tools(self):
        return _FakeMcpListing(self._tools)


def _make_real_client(tools):
    """Build a real ``AssistantMcpClient`` instance with a fake MCP
    session attached, bypassing the SSE connect path.  Uses
    ``__new__`` to skip ``__init__`` (which has hard requirements on
    ``settings`` + ``user``)."""
    from cms.services.assistant.mcp_client import AssistantMcpClient

    client = AssistantMcpClient.__new__(AssistantMcpClient)
    client._session = _FakeMcpSession(tools)  # type: ignore[attr-defined]
    return client


@pytest.mark.asyncio
class TestMcpToolExposure:
    """``list_openai_tools`` must expose BOTH read and curated write
    tools to the LLM, while filtering anything outside the allowlist.
    """

    async def test_exposes_curated_write_tools(self):
        """At least one write tool from each category we curate must
        survive the filter.  Regression for #670 (the filter was
        ``READ_ONLY_TOOLS`` not ``ALLOWED_TOOLS``).
        """
        tools = [
            _FakeMcpTool("list_devices"),
            _FakeMcpTool("create_schedule"),
            _FakeMcpTool("update_asset"),
            _FakeMcpTool("delete_group"),
            _FakeMcpTool("adopt_device"),
            _FakeMcpTool("create_tag"),
        ]
        exposed = await _make_real_client(tools).list_openai_tools()
        names = {t["function"]["name"] for t in exposed}

        for write in ("create_schedule", "update_asset", "delete_group",
                      "adopt_device", "create_tag"):
            assert write in names, (
                f"write tool {write!r} missing from LLM tool catalog "
                f"(#670 regression); saw: {names}"
            )

    async def test_exposes_read_tools(self):
        tools = [
            _FakeMcpTool("list_devices"),
            _FakeMcpTool("list_schedules"),
            _FakeMcpTool("get_dashboard"),
        ]
        exposed = await _make_real_client(tools).list_openai_tools()
        names = {t["function"]["name"] for t in exposed}
        assert names == {"list_devices", "list_schedules", "get_dashboard"}

    async def test_filters_out_non_allowlisted_tools(self):
        """Destructive / security-sensitive tools must NEVER reach
        the LLM — they're deliberately excluded from ``ALLOWED_TOOLS``.
        """
        tools = [
            _FakeMcpTool("list_devices"),       # read — allowed
            _FakeMcpTool("delete_device"),      # destructive — excluded
            _FakeMcpTool("factory_reset_device"),  # destructive — excluded
            _FakeMcpTool("set_device_password"),   # security — excluded
            _FakeMcpTool("set_ssh_enabled"),       # security — excluded
        ]
        names = {
            t["function"]["name"]
            for t in await _make_real_client(tools).list_openai_tools()
        }
        assert names == {"list_devices"}, (
            f"Expected only list_devices to survive; got {names}. "
            "Destructive/security-sensitive tools must never reach the LLM."
        )

    async def test_write_tool_descriptions_carry_approval_note(self):
        """LLM needs to know write tools queue an approval, not
        execute instantly — otherwise it promises the user the wrong
        thing ("done!" before the user clicks Approve).
        """
        tools = [
            _FakeMcpTool("create_schedule", description="Create a schedule."),
            _FakeMcpTool("list_devices", description="List devices."),
        ]
        by_name = {
            t["function"]["name"]: t["function"]["description"]
            for t in await _make_real_client(tools).list_openai_tools()
        }
        assert "approv" in by_name["create_schedule"].lower(), (
            "write tool descriptions must surface the approval contract; "
            f"got: {by_name['create_schedule']!r}"
        )
        assert "approv" not in by_name["list_devices"].lower(), (
            "read tool descriptions must NOT mention approval (sets "
            "false expectations)"
        )


# ── End-to-end pipeline contract ────────────────────────────────────


async def _consume_sse(response):
    """Parse a streaming SSE response into a list of (event, data) tuples."""
    events: list[tuple[str, object]] = []
    cur_event: str | None = None
    async for raw in response.aiter_lines():
        line = raw.rstrip("\r")
        if not line:
            cur_event = None
            continue
        if line.startswith("event:"):
            cur_event = line[len("event:"):].strip()
        elif line.startswith("data:"):
            payload = line[len("data:"):].strip()
            try:
                data = json.loads(payload)
            except json.JSONDecodeError:
                data = payload
            events.append((cur_event or "message", data))
    return events


@pytest.mark.asyncio
class TestStreamingPipelineContract:
    """Exercise the whole streaming pipeline end-to-end, asserting
    cross-module contracts (system prompt + tool exposure + approval
    intercept) are wired together correctly.
    """

    async def test_stream_passes_both_read_and_write_tools_to_llm(
        self, client, scripted_agent
    ):
        """The agent loop must hand the LLM a tool catalog that
        contains BOTH read AND write tools.  Cleanest regression for
        #670 — even with the right prompt and right filter, a refactor
        could skip the ``list_openai_tools`` call entirely.
        """
        scripted_agent["mcp_tools"] = [
            {
                "type": "function",
                "function": {
                    "name": "list_devices",
                    "description": "List devices.",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "create_schedule",
                    "description": "Create a schedule.",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
        ]
        scripted_agent["llm_replies"] = [_FakeResult(content="ok", tokens_in=10, tokens_out=5)]

        tid = await _create_thread(client, title="smoke")
        async with client.stream(
            "POST",
            f"/api/chat/threads/{tid}/stream",
            json={"content": "list my devices"},
        ) as resp:
            assert resp.status_code == 200, await resp.aread()
            await _consume_sse(resp)

        llm = FakeLLMClient.last_instance
        assert llm is not None, "FakeLLMClient was not constructed"
        assert llm.tools_seen, "agent never passed `tools=` to LLM.stream"

        tool_names = {
            t["function"]["name"]
            for call in llm.tools_seen if call
            for t in call
        }
        assert "list_devices" in tool_names, (
            f"read tools missing from LLM call; saw: {tool_names}"
        )
        assert "create_schedule" in tool_names, (
            f"write tools missing from LLM call (#670 regression); "
            f"saw: {tool_names}"
        )

    async def test_stream_uses_real_system_prompt(self, client, scripted_agent):
        """The streaming agent must inject the real
        ``build_system_prompt`` output as the system message — not a
        stub.  Combined with the system-prompt contract tests above,
        this catches #667.
        """
        scripted_agent["llm_replies"] = [_FakeResult(content="ok", tokens_in=10, tokens_out=5)]

        tid = await _create_thread(client, title="smoke")
        async with client.stream(
            "POST",
            f"/api/chat/threads/{tid}/stream",
            json={"content": "ping"},
        ) as resp:
            assert resp.status_code == 200, await resp.aread()
            await _consume_sse(resp)

        llm = FakeLLMClient.last_instance
        assert llm.calls, "LLM was never called"
        system_msg = llm.calls[0][0]
        assert system_msg["role"] == "system"
        # Must be the real prompt — contains the Assistant header and
        # mentions tools (not a stub like "you are a chatbot").
        assert "Agora CMS Assistant" in system_msg["content"]
        assert "tools" in system_msg["content"].lower()
