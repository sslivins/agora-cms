"""Tests for assistant tool-scoping by thread ``mode`` (PR 1).

The composed-slide editor embeds an assistant that runs in a dedicated
thread ``mode`` ("composed_editor").  That mode exposes a small,
purpose-built tool profile (asset discovery + composed read/write) and
NEVER the general device/schedule/profile fleet-management tools.  The
general chat tab ("general" mode) keeps its existing behaviour and is
never able to see or run the composed tools.

These tests pin the scoping contract at three levels:

* pure-function: ``tools_for_mode`` / ``executable_tools_for_mode``.
* ``list_openai_tools(mode=...)``: the LLM-facing catalog is filtered by
  mode, using a real ``AssistantMcpClient`` over a fake MCP session.
* invariants: composed draft writes run inline (no approval) while fleet
  writes never leak into editor mode.
"""

from __future__ import annotations

import pytest

from cms.services.assistant.mcp_client import (
    ALLOWED_TOOLS,
    COMPOSED_DRAFT_WRITE_TOOLS,
    COMPOSED_EDITOR_TOOLS,
    COMPOSED_READ_TOOLS,
    MODE_COMPOSED_EDITOR,
    MODE_GENERAL,
    NO_APPROVAL_TOOLS,
    READ_ONLY_TOOLS,
    WRITE_TOOLS,
    AssistantMcpClient,
    executable_tools_for_mode,
    tools_for_mode,
)


# Reuse the fake MCP session doubles from the smoke suite.
from tests.test_chat_smoke import _FakeMcpTool, _make_real_client


# ── pure-function scoping ───────────────────────────────────────────


class TestModeProfiles:
    def test_composed_tools_excluded_from_general_whitelists(self):
        """The composed tools must live OUTSIDE the general whitelists
        so the general chat tab can neither advertise nor execute them.
        """
        composed = COMPOSED_READ_TOOLS | COMPOSED_DRAFT_WRITE_TOOLS
        assert not (composed & READ_ONLY_TOOLS)
        assert not (composed & WRITE_TOOLS)
        assert not (composed & ALLOWED_TOOLS)

    def test_general_mode_is_allowed_tools(self):
        assert tools_for_mode(MODE_GENERAL) == ALLOWED_TOOLS

    def test_unknown_or_none_mode_falls_back_to_general(self):
        # A malformed mode must never WIDEN access.
        assert tools_for_mode(None) == ALLOWED_TOOLS
        assert tools_for_mode("nonsense") == ALLOWED_TOOLS

    def test_editor_mode_is_scoped_profile(self):
        prof = tools_for_mode(MODE_COMPOSED_EDITOR)
        assert prof == COMPOSED_EDITOR_TOOLS
        # Exactly: asset discovery + composed read/write.
        assert {"list_assets", "get_asset"} <= prof
        assert COMPOSED_READ_TOOLS <= prof
        assert COMPOSED_DRAFT_WRITE_TOOLS <= prof

    def test_editor_mode_excludes_fleet_tools(self):
        prof = tools_for_mode(MODE_COMPOSED_EDITOR)
        for fleet in (
            "list_devices",
            "create_schedule",
            "update_asset",
            "delete_group",
            "list_audit_logs",
        ):
            assert fleet not in prof, f"{fleet!r} leaked into editor mode"

    def test_composed_draft_write_runs_inline(self):
        """``set_composed_widgets`` is a draft-only write: it must be in
        the no-approval floor AND executable in editor mode (inline, no
        approval click)."""
        assert COMPOSED_DRAFT_WRITE_TOOLS <= NO_APPROVAL_TOOLS
        execable = executable_tools_for_mode(MODE_COMPOSED_EDITOR)
        assert "set_composed_widgets" in execable
        # ...and it is NOT in the fleet WRITE_TOOLS, so the streaming
        # approval gate never fires for it.
        assert "set_composed_widgets" not in WRITE_TOOLS

    def test_general_mode_executes_only_reads_inline(self):
        execable = executable_tools_for_mode(MODE_GENERAL)
        # Reads run inline; no fleet write is inline-executable.
        assert READ_ONLY_TOOLS <= execable
        assert not (execable & WRITE_TOOLS)
        # Composed tools are not reachable at all in general mode.
        assert not (execable & COMPOSED_DRAFT_WRITE_TOOLS)

    def test_executable_is_subset_of_advertised(self):
        for mode in (MODE_GENERAL, MODE_COMPOSED_EDITOR):
            assert executable_tools_for_mode(mode) <= tools_for_mode(mode)


# ── list_openai_tools(mode=...) over a real client ──────────────────


@pytest.mark.asyncio
class TestListOpenAiToolsByMode:
    def _mixed_tools(self):
        # A mix of fleet read/write tools and the composed tools, plus
        # the asset tools the editor profile keeps.
        return [
            _FakeMcpTool("list_devices"),
            _FakeMcpTool("create_schedule"),
            _FakeMcpTool("list_assets"),
            _FakeMcpTool("get_asset"),
            _FakeMcpTool("list_composed_widget_types"),
            _FakeMcpTool("get_composed_layout"),
            _FakeMcpTool("set_composed_widgets"),
        ]

    async def test_general_mode_excludes_composed_tools(self):
        client = _make_real_client(self._mixed_tools())
        names = {
            t["function"]["name"]
            for t in await client.list_openai_tools(mode=MODE_GENERAL)
        }
        assert "list_devices" in names
        assert "create_schedule" in names
        for composed in (
            "list_composed_widget_types",
            "get_composed_layout",
            "set_composed_widgets",
        ):
            assert composed not in names, f"{composed!r} leaked into general mode"

    async def test_default_mode_matches_general(self):
        client = _make_real_client(self._mixed_tools())
        default = {
            t["function"]["name"] for t in await client.list_openai_tools()
        }
        general = {
            t["function"]["name"]
            for t in await client.list_openai_tools(mode=MODE_GENERAL)
        }
        assert default == general

    async def test_editor_mode_exposes_only_editor_profile(self):
        client = _make_real_client(self._mixed_tools())
        names = {
            t["function"]["name"]
            for t in await client.list_openai_tools(mode=MODE_COMPOSED_EDITOR)
        }
        assert names == {
            "list_assets",
            "get_asset",
            "list_composed_widget_types",
            "get_composed_layout",
            "set_composed_widgets",
        }
        # No fleet tools.
        assert "list_devices" not in names
        assert "create_schedule" not in names

    async def test_composed_write_has_no_approval_note_in_editor(self):
        """``set_composed_widgets`` is a draft-only write — its
        description must NOT carry the approval note that fleet writes
        get, or the LLM will tell the user to click Approve when there's
        nothing to approve."""
        client = _make_real_client([
            _FakeMcpTool("set_composed_widgets", description="Set widgets."),
        ])
        by_name = {
            t["function"]["name"]: t["function"]["description"]
            for t in await client.list_openai_tools(mode=MODE_COMPOSED_EDITOR)
        }
        assert "approv" not in by_name["set_composed_widgets"].lower()
