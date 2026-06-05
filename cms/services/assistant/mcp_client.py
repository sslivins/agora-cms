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


# Write/mutating tools exposed to the assistant.  Each one is gated by
# the PR-4 approval flow: when the LLM calls one of these, the agent
# loop intercepts, persists a ``ChatPendingApproval`` row, and emits
# an ``approval_request`` SSE event so the UI can render an
# Approve / Reject card.  The tool only runs after the user clicks
# Approve, and it runs with ``bypass_whitelist=True`` from the
# chat_approvals router.
#
# Truly destructive or security-sensitive operations are deliberately
# left off this list and remain UI-only.  Add a tool here only after
# confirming that a malicious-prompt-induced invocation, intercepted
# by approval, would still be obviously wrong to the human reviewer
# (i.e. the tool's name + arguments make its impact clear).
WRITE_TOOLS: frozenset[str] = frozenset(
    {
        # devices — onboarding / lifecycle (NOT delete, factory-reset,
        # set-password, ssh/local-api toggles)
        "adopt_device",
        "update_device",
        "reboot_device",
        "check_device_updates",
        "upgrade_device",
        # groups
        "create_group",
        "update_group",
        "delete_group",
        # assets (non-destructive + delete; webpage assets are user-authored)
        "create_webpage_asset",
        "update_asset",
        "delete_asset",
        "share_asset",
        "unshare_asset",
        "toggle_asset_global",
        "recapture_stream",
        # tags
        "create_tag",
        "update_tag",
        "delete_tag",
        # schedules
        "create_schedule",
        "update_schedule",
        "delete_schedule",
        "play_now",
        "end_schedule_now",
        # profiles
        "create_profile",
        "update_profile",
        "copy_profile",
        "enable_profile",
        "disable_profile",
        "reset_profile",
        "delete_profile",
        # asset views (per-user saved filters; low-risk)
        "create_asset_view",
        "update_asset_view",
        "delete_asset_view",
    }
)

# Combined set used both for tool exposure and for the approval gate
# in :class:`cms.services.assistant.agent`.
ALLOWED_TOOLS: frozenset[str] = READ_ONLY_TOOLS | WRITE_TOOLS


# ── Composed-slide editor mode ────────────────────────────────────────
#
# The composed-slide editor embeds an assistant whose job is to build
# the *draft* layout of the one slide the user is editing.  It runs in a
# dedicated thread ``mode`` ("composed_editor") that exposes a small,
# purpose-built tool profile and NEVER the general device/schedule/
# profile fleet-management tools.
#
# These tools are deliberately kept OUT of ``READ_ONLY_TOOLS`` /
# ``WRITE_TOOLS`` / ``ALLOWED_TOOLS`` so the general chat tab can neither
# advertise nor execute them.  They are only reachable when the thread's
# mode selects the editor profile below.
MODE_GENERAL = "general"
MODE_COMPOSED_EDITOR = "composed_editor"
VALID_MODES: frozenset[str] = frozenset({MODE_GENERAL, MODE_COMPOSED_EDITOR})

# Composed reads — safe to run inline like any other read.
COMPOSED_READ_TOOLS: frozenset[str] = frozenset(
    {
        "list_composed_widget_types",
        "get_composed_layout",
    }
)

# Composed draft writes — these mutate ONLY the unpublished draft layout
# of a composed slide.  Publishing a slide to devices is a separate,
# human-only UI button (never an MCP tool), so a draft write is fully
# reversible and carries no fleet impact.  They therefore run inline
# WITHOUT an approval click, exactly like reads.
COMPOSED_DRAFT_WRITE_TOOLS: frozenset[str] = frozenset(
    {
        "set_composed_widgets",
    }
)

# Tools the agent may execute immediately, with no approval click.
# Reads are inherently safe; composed draft writes are reversible and
# device-invisible (see above).
NO_APPROVAL_TOOLS: frozenset[str] = (
    READ_ONLY_TOOLS | COMPOSED_READ_TOOLS | COMPOSED_DRAFT_WRITE_TOOLS
)

# The editor-mode tool profile: just enough to discover/select assets
# and read+write the current slide's draft layout.  No device, schedule,
# profile, log, or audit tools.
COMPOSED_EDITOR_TOOLS: frozenset[str] = (
    frozenset({"list_assets", "get_asset"})
    | COMPOSED_READ_TOOLS
    | COMPOSED_DRAFT_WRITE_TOOLS
)


def tools_for_mode(mode: str | None) -> frozenset[str]:
    """Return the set of tools advertised/callable for a thread ``mode``.

    Unknown or ``None`` modes fall back to the general profile so a
    malformed mode value can never *widen* access.
    """
    if mode == MODE_COMPOSED_EDITOR:
        return COMPOSED_EDITOR_TOOLS
    return ALLOWED_TOOLS


def executable_tools_for_mode(mode: str | None) -> frozenset[str]:
    """No-approval tools the agent may run directly in ``mode``.

    Intersection of the mode's profile with the global no-approval
    floor — so editor mode runs its composed tools inline, and general
    mode keeps running only its reads inline (writes still go through
    the approval flow).
    """
    return NO_APPROVAL_TOOLS & tools_for_mode(mode)


def _read_service_key(settings: Settings) -> str:
    """Read the MCP service key.

    In local/Docker-Compose deployments CMS and MCP share a volume and
    the key lives at ``settings.service_key_path``. In Azure Container
    Apps there's no shared volume, so the key is exchanged via Key
    Vault (see ``cms.keyvault.write_key_to_keyvault``).

    Tries the local file first; if it's missing or empty and
    ``azure_keyvault_uri`` is configured, falls back to Key Vault.
    Raises :class:`McpUnavailableError` if neither path yields a key.
    """
    file_err: str | None = None
    try:
        raw = Path(settings.service_key_path).read_text().strip()
        if raw:
            return raw
        file_err = (
            f"MCP service key file at {settings.service_key_path} is "
            "empty (key not provisioned?)"
        )
    except OSError as exc:
        file_err = (
            f"MCP service key file is not readable at "
            f"{settings.service_key_path}: {exc}"
        )

    if settings.azure_keyvault_uri:
        from cms.keyvault import read_key_from_keyvault

        kv_key = read_key_from_keyvault(settings.azure_keyvault_uri).strip()
        if kv_key:
            return kv_key
        raise McpUnavailableError(
            f"MCP service key not available: {file_err}; Key Vault "
            f"{settings.azure_keyvault_uri} returned empty value for "
            "'mcp-service-key' (key not provisioned?)"
        )

    raise McpUnavailableError(file_err or "MCP service key not available")


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

        raw_key = _read_service_key(self._settings)
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

    async def list_openai_tools(
        self, mode: str = MODE_GENERAL
    ) -> list[dict[str, Any]]:
        """Return MCP tools filtered to the ``mode`` profile, in OpenAI format.

        ``mode`` selects which tool profile is advertised to the LLM:
        ``"general"`` (default) exposes the full read + approval-gated
        write fleet tools; ``"composed_editor"`` exposes only the small
        composed-slide editor profile.

        OpenAI's tool schema is:
        ``{"type":"function", "function":{"name":..., "description":..., "parameters":{...}}}``
        which maps cleanly from MCP's ``Tool.inputSchema`` JSON-Schema.
        """
        if self._session is None:  # pragma: no cover
            raise RuntimeError("AssistantMcpClient not entered")
        exposed = tools_for_mode(mode)
        listing = await self._session.list_tools()
        out: list[dict[str, Any]] = []
        for tool in listing.tools:
            if tool.name not in exposed:
                continue
            description = tool.description or ""
            if tool.name in WRITE_TOOLS:
                # Surface the approval contract in the description the
                # LLM sees so it can set expectations with the user
                # ("I'll create that schedule — you'll see an approve
                # button") rather than promising instant execution.
                description = (
                    f"{description}\n\n"
                    "[Note: this is a write tool — calling it will queue "
                    "an approval request that the user must explicitly "
                    "click Approve on before it runs.]"
                ).strip()
            out.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": description,
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
        if not bypass_whitelist and name not in NO_APPROVAL_TOOLS:
            raise PermissionError(
                f"Tool '{name}' is not in the no-approval whitelist "
                "(fleet writes require approval)."
            )
        if bypass_whitelist and name not in ALLOWED_TOOLS:
            # Defence in depth: the approval router calls us with
            # bypass=True only after a human clicked Approve, but we
            # must still refuse anything outside the explicit
            # WRITE_TOOLS allowlist so a hallucinated or out-of-scope
            # tool name can't slip through just because someone hit
            # the button on the card.
            raise PermissionError(
                f"Tool '{name}' is not in the assistant's allowlist "
                "(read or write)."
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
