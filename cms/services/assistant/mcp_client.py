"""MCP client for the Assistant agent (PR 3b).

Opens an SSE connection to the in-cluster MCP server using the same
``agora_svc_*`` service key shared with the MCP container's
``/reload-key`` path, plus an ``X-On-Behalf-Of: <user uuid>`` header so
that per-user RBAC is preserved end-to-end (see PR #656).

Design notes:

* The agent owns one ``AssistantMcpClient`` instance per user turn, used
  as an async context manager.  Setup cost is the SSE handshake + MCP
  ``initialize`` round-trip — small, but enough that we don't pay it on
  every tool call within a turn.
* Only **read-only** tools are exposed to the LLM in Phase 1.  Writes
  land in a later PR once the approval flow exists; until then, even if
  the LLM hallucinates a write tool name we refuse it at the wrapper
  before hitting MCP.  The whitelist is hand-curated against
  :data:`mcp.server.TOOL_PERMISSIONS` so a new write tool doesn't
  silently become callable just because it's exposed by MCP.
* MCP unreachable is a hard failure, not a degrade — the agent loop
  treats it the same as Azure OpenAI being down and surfaces a 503 to
  the caller via :class:`McpUnavailableError`.  We don't want to give
  the user a chatty answer with no tool grounding when the whole point
  of the feature is the tool grounding.
"""
from __future__ import annotations

import json
import logging
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from cms.config import Settings
from cms.models.user import User

logger = logging.getLogger(__name__)


class McpUnavailableError(RuntimeError):
    """Raised when the MCP server cannot be reached or initialized.

    The chat router converts this to ``503 Service Unavailable``.
    """


# Read-only tool whitelist — manually curated against
# ``mcp/server.py::TOOL_PERMISSIONS``.  Includes every tool whose
# required permission ends in ``:read`` plus the small set of
# ``None``-permission tools that are read-only by construction.
#
# When ``mcp/server.py`` gains a new tool, it does NOT automatically
# show up here — that's deliberate.  A human has to decide whether the
# new tool is safe to expose to the LLM without approval, and add it
# below.  PR 4 will replace this list with a "any tool, but writes
# require approval" model.
READ_ONLY_TOOLS: frozenset[str] = frozenset(
    {
        # devices:read
        "list_devices",
        "get_device",
        # groups:read
        "list_groups",
        # assets:read
        "list_assets",
        "get_asset",
        "list_assets_paged",
        "list_tags",
        # schedules:read
        "list_schedules",
        "get_schedule",
        # profiles:read
        "list_profiles",
        # logs:read
        "get_device_logs",
        # audit:read
        "list_audit_events",
        # None (any authenticated user)
        "get_server_time",
        "get_dashboard",
        "list_asset_views",
    }
)


def _read_service_key(path: str) -> str:
    """Read the MCP service key off the shared volume.

    Returns the raw ``agora_svc_*`` string.  Raises
    :class:`McpUnavailableError` if the file is missing or empty — both
    are fatal: there is no fallback authentication path.
    """
    try:
        raw = Path(path).read_text().strip()
    except OSError as exc:
        raise McpUnavailableError(
            f"MCP service key file is not readable at {path}: {exc}"
        ) from exc
    if not raw:
        raise McpUnavailableError(
            f"MCP service key file at {path} is empty (key not provisioned?)"
        )
    return raw


class AssistantMcpClient:
    """Async wrapper around ``mcp.client.sse.sse_client`` + ``ClientSession``.

    Use as an async context manager — connection + initialize happen in
    ``__aenter__``, teardown in ``__aexit__``.  Construction is cheap
    (no I/O); all network work is deferred.
    """

    def __init__(self, *, settings: Settings, user: User) -> None:
        self._settings = settings
        self._user = user
        self._stack = AsyncExitStack()
        self._session: Any = None  # mcp.client.session.ClientSession

    async def __aenter__(self) -> "AssistantMcpClient":
        # Import inside __aenter__ so a broken ``mcp`` install doesn't
        # take out import-time for the whole CMS process.
        from mcp.client.session import ClientSession
        from mcp.client.sse import sse_client

        raw_key = _read_service_key(self._settings.service_key_path)
        url = self._settings.mcp_server_url.rstrip("/") + "/sse"
        headers = {
            "Authorization": f"Bearer {raw_key}",
            "X-On-Behalf-Of": str(self._user.id),
        }
        logger.info(
            "assistant.mcp.connect url=%s user=%s",
            url,
            self._user.id,
        )
        try:
            streams = await self._stack.enter_async_context(
                sse_client(url, headers=headers)
            )
            # sse_client yields (read_stream, write_stream).  Newer SDK
            # versions yield a 3-tuple including a write callback; we
            # only need the first two.
            read_stream, write_stream = streams[0], streams[1]
            session = await self._stack.enter_async_context(
                ClientSession(read_stream, write_stream)
            )
            await session.initialize()
        except McpUnavailableError:
            await self._stack.aclose()
            raise
        except Exception as exc:
            await self._stack.aclose()
            raise McpUnavailableError(
                f"MCP connection failed: {type(exc).__name__}: {exc}"
            ) from exc
        self._session = session
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        try:
            await self._stack.aclose()
        except Exception:  # pragma: no cover — defensive
            logger.exception("assistant.mcp.close_failed")

    async def list_openai_tools(self) -> list[dict[str, Any]]:
        """Return MCP tools filtered to the read-only whitelist, in OpenAI format.

        OpenAI's tool schema is:
        ``{"type":"function", "function":{"name":..., "description":..., "parameters":{...}}}``
        which maps cleanly from MCP's ``Tool.inputSchema`` JSON-Schema.
        """
        if self._session is None:  # pragma: no cover
            raise RuntimeError("AssistantMcpClient not entered")
        listing = await self._session.list_tools()
        out: list[dict[str, Any]] = []
        for tool in listing.tools:
            if tool.name not in READ_ONLY_TOOLS:
                continue
            out.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description or "",
                        "parameters": tool.inputSchema
                        or {"type": "object", "properties": {}},
                    },
                }
            )
        logger.info(
            "assistant.mcp.list_tools total=%d exposed=%d",
            len(listing.tools),
            len(out),
        )
        return out

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        bypass_whitelist: bool = False,
    ) -> str:
        """Invoke an MCP tool and return its result as a JSON-string.

        Raises :class:`PermissionError` if ``name`` isn't in the
        read-only whitelist — the agent loop catches this and turns it
        into a synthetic tool-result the LLM can reason about.

        ``bypass_whitelist=True`` is the **explicit** opt-out used by
        the PR 4 approval flow: when the user has clicked Approve on a
        pending write-tool approval, the chat_approvals router runs
        the tool with the whitelist disabled.  The whitelist exists to
        prevent the LLM running writes without a human in the loop;
        the approval click IS that human in the loop.  Never set this
        flag without an approved :class:`ChatPendingApproval` row.
        """
        if self._session is None:  # pragma: no cover
            raise RuntimeError("AssistantMcpClient not entered")
        if not bypass_whitelist and name not in READ_ONLY_TOOLS:
            raise PermissionError(
                f"Tool '{name}' is not in the read-only whitelist "
                "(writes require approval; see PR 4)."
            )
        logger.info(
            "assistant.mcp.call name=%s user=%s bypass=%s",
            name,
            self._user.id,
            bypass_whitelist,
        )
        result = await self._session.call_tool(name, arguments)
        # Prefer structuredContent if the server sent it.
        if getattr(result, "structuredContent", None) is not None:
            return json.dumps(result.structuredContent, default=str)
        # Fall back to concatenated text blocks.
        parts: list[str] = []
        for block in result.content or []:
            text = getattr(block, "text", None)
            if text is not None:
                parts.append(text)
            else:
                dump = (
                    block.model_dump()
                    if hasattr(block, "model_dump")
                    else str(block)
                )
                parts.append(json.dumps(dump, default=str))
        body = "\n".join(parts)
        if getattr(result, "isError", False):
            return json.dumps({"error": "tool_error", "message": body})
        return body
