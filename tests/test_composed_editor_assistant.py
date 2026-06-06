"""Tests for the composed-editor embedded AI assistant (PR 2).

Covers the server-side pieces that back the chat panel embedded in the
composed-slide editor:

* ``POST /composed/{id}/assistant/thread`` — get-or-create the
  editor-scoped, asset-bound chat thread.
* ``list_threads`` excludes ``composed_editor`` threads from the
  general assistant sidebar.
* ``build_system_prompt`` renders the slide-editor variant only in
  ``composed_editor`` mode with a bound asset id (never widens).
* ``_execute_tool_call`` forces the bound slide id onto the composed
  asset-scoped tools and leaves everything else untouched.
* ``_verify_composed_editor_thread`` re-validates an editor thread at
  turn start (orphaned → 409, permission lost → 403, general → no-op).
"""

from __future__ import annotations

import json
import types
import uuid

import pytest
from sqlalchemy import select

from cms.composed.schema import empty_layout
from cms.models.asset import Asset, AssetType
from cms.models.chat_thread import ChatThread
from cms.models.composed_slide import ComposedSlide
from cms.services.assistant.mcp_client import MODE_COMPOSED_EDITOR, MODE_GENERAL


# ── fixtures / helpers ──────────────────────────────────────────────


async def _make_composed(
    db_session, *, owner_id=None, is_global: bool = False,
) -> Asset:
    asset = Asset(
        filename=f"composed-{uuid.uuid4()}",
        display_name="Test composed",
        asset_type=AssetType.COMPOSED,
        size_bytes=0,
        checksum="",
        uploaded_by_user_id=owner_id,
        is_global=is_global,
    )
    db_session.add(asset)
    await db_session.flush()
    cs = ComposedSlide(
        asset_id=asset.id,
        layout_json=empty_layout().model_dump(mode="json"),
        is_draft=True,
    )
    db_session.add(cs)
    await db_session.commit()
    return asset


async def _disable_all(app) -> None:
    from cms.database import get_db
    from cms.services.assistant_flag import set_allowlist

    factory = app.dependency_overrides[get_db]
    async for db in factory():
        await set_allowlist(db, [])
        break


# ── POST /composed/{id}/assistant/thread ────────────────────────────


@pytest.mark.asyncio
class TestAssistantThreadEndpoint:
    async def test_creates_then_reuses_same_thread(self, client, db_session):
        asset = await _make_composed(db_session)

        first = await client.post(f"/composed/{asset.id}/assistant/thread")
        assert first.status_code == 200, first.text
        b1 = first.json()
        assert b1["created"] is True
        uuid.UUID(b1["thread_id"])

        second = await client.post(f"/composed/{asset.id}/assistant/thread")
        assert second.status_code == 200, second.text
        b2 = second.json()
        assert b2["created"] is False
        assert b2["thread_id"] == b1["thread_id"]

    async def test_thread_is_bound_and_in_editor_mode(self, client, db_session):
        asset = await _make_composed(db_session)
        resp = await client.post(f"/composed/{asset.id}/assistant/thread")
        assert resp.status_code == 200, resp.text
        tid = uuid.UUID(resp.json()["thread_id"])

        row = (
            await db_session.execute(
                select(ChatThread).where(ChatThread.id == tid)
            )
        ).scalar_one()
        assert row.mode == MODE_COMPOSED_EDITOR
        assert row.composed_asset_id == asset.id

    async def test_missing_asset_is_404(self, client):
        resp = await client.post(
            f"/composed/{uuid.uuid4()}/assistant/thread"
        )
        assert resp.status_code == 404

    async def test_feature_off_is_404(self, operator_client, app, db_session):
        # Operator owns the slide (so it's visible), but the Assistant
        # feature is off → the endpoint must 404 to keep it invisible.
        await _disable_all(app)
        asset = await _make_composed(
            db_session, owner_id=operator_client.user_id
        )
        resp = await operator_client.post(
            f"/composed/{asset.id}/assistant/thread"
        )
        assert resp.status_code == 404

    async def test_unauth_is_401(self, unauthed_client, db_session):
        asset = await _make_composed(db_session)
        resp = await unauthed_client.post(
            f"/composed/{asset.id}/assistant/thread"
        )
        assert resp.status_code in (401, 403)


# ── list_threads excludes editor threads ────────────────────────────


@pytest.mark.asyncio
class TestListThreadsExcludesEditor:
    async def test_editor_thread_hidden_from_sidebar(self, client, db_session):
        # A normal general thread shows up…
        general = await client.post("/api/chat/threads", json={"title": "Promo"})
        assert general.status_code == 201
        general_id = general.json()["id"]

        # …an editor-bound thread does not.
        asset = await _make_composed(db_session)
        editor = await client.post(f"/composed/{asset.id}/assistant/thread")
        editor_id = editor.json()["thread_id"]

        listing = (await client.get("/api/chat/threads")).json()
        ids = {t["id"] for t in listing}
        assert general_id in ids
        assert editor_id not in ids


# ── build_system_prompt mode awareness ──────────────────────────────


class TestBuildSystemPrompt:
    def _user(self):
        return types.SimpleNamespace(username="alice", email="alice@test.com")

    def test_composed_mode_renders_editor_prompt_with_bound_id(self):
        from cms.services.assistant.prompts import build_system_prompt

        aid = str(uuid.uuid4())
        prompt = build_system_prompt(
            self._user(), mode="composed_editor", composed_asset_id=aid
        )
        assert aid in prompt
        assert "Composed-Slide Assistant" in prompt
        assert "set_composed_widgets" in prompt
        # Editor prompt must NOT advertise fleet tooling.
        assert "create_schedule" not in prompt

    def test_composed_mode_without_id_falls_back_to_general(self):
        from cms.services.assistant.prompts import build_system_prompt

        prompt = build_system_prompt(
            self._user(), mode="composed_editor", composed_asset_id=None
        )
        assert "Composed-Slide Assistant" not in prompt

    def test_unknown_mode_does_not_widen(self):
        from cms.services.assistant.prompts import build_system_prompt

        prompt = build_system_prompt(
            self._user(), mode="nonsense", composed_asset_id=str(uuid.uuid4())
        )
        assert "Composed-Slide Assistant" not in prompt

    def test_default_mode_is_general(self):
        from cms.services.assistant.prompts import build_system_prompt

        prompt = build_system_prompt(self._user())
        assert "Composed-Slide Assistant" not in prompt


# ── _execute_tool_call bound-asset override ──────────────────────────


class _RecordingMcp:
    """Minimal AssistantMcpClient double that records call_tool args."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, name, args):
        self.calls.append((name, dict(args or {})))
        return "{}"


def _tool_call(name: str, **args) -> dict:
    return {"function": {"name": name, "arguments": json.dumps(args)}}


@pytest.mark.asyncio
class TestExecuteToolCallBinding:
    async def test_composed_scoped_tool_forces_bound_id(self):
        from cms.services.assistant.agent import _execute_tool_call

        bound = str(uuid.uuid4())
        mcp = _RecordingMcp()
        await _execute_tool_call(
            mcp=mcp,
            tool_call=_tool_call(
                "set_composed_widgets", asset_id=str(uuid.uuid4()), widgets=[]
            ),
            executable_tools=frozenset({"set_composed_widgets"}),
            bound_asset_id=bound,
        )
        assert mcp.calls and mcp.calls[0][1]["asset_id"] == bound

    async def test_non_scoped_tool_left_untouched(self):
        from cms.services.assistant.agent import _execute_tool_call

        bound = str(uuid.uuid4())
        mcp = _RecordingMcp()
        await _execute_tool_call(
            mcp=mcp,
            tool_call=_tool_call("list_assets", asset_type="image"),
            executable_tools=frozenset({"list_assets"}),
            bound_asset_id=bound,
        )
        assert mcp.calls and "asset_id" not in mcp.calls[0][1]

    async def test_no_binding_keeps_model_asset_id(self):
        from cms.services.assistant.agent import _execute_tool_call

        model_id = str(uuid.uuid4())
        mcp = _RecordingMcp()
        await _execute_tool_call(
            mcp=mcp,
            tool_call=_tool_call(
                "set_composed_widgets", asset_id=model_id, widgets=[]
            ),
            executable_tools=frozenset({"set_composed_widgets"}),
            bound_asset_id=None,
        )
        assert mcp.calls and mcp.calls[0][1]["asset_id"] == model_id


# ── _verify_composed_editor_thread ──────────────────────────────────


@pytest.mark.asyncio
class TestVerifyComposedEditorThread:
    async def test_general_thread_is_noop(self):
        from cms.routers.chat import _verify_composed_editor_thread

        thread = ChatThread(
            user_id=uuid.uuid4(), mode=MODE_GENERAL, title="x"
        )
        # request/db unused on the general no-op path.
        await _verify_composed_editor_thread(thread, None, None, None)

    async def test_orphaned_editor_thread_is_409(self):
        from fastapi import HTTPException

        from cms.routers.chat import _verify_composed_editor_thread

        thread = ChatThread(
            user_id=uuid.uuid4(),
            mode=MODE_COMPOSED_EDITOR,
            composed_asset_id=None,
            title="x",
        )
        with pytest.raises(HTTPException) as ei:
            await _verify_composed_editor_thread(thread, None, None, None)
        assert ei.value.status_code == 409

    async def test_permission_lost_is_403(self):
        from fastapi import HTTPException

        from cms.routers.chat import _verify_composed_editor_thread

        thread = ChatThread(
            user_id=uuid.uuid4(),
            mode=MODE_COMPOSED_EDITOR,
            composed_asset_id=uuid.uuid4(),
            title="x",
        )
        # Role without ASSETS_WRITE → 403 before any DB/visibility work.
        viewer = types.SimpleNamespace(
            role=types.SimpleNamespace(permissions=["assets:read"])
        )
        with pytest.raises(HTTPException) as ei:
            await _verify_composed_editor_thread(thread, viewer, None, None)
        assert ei.value.status_code == 403

# ── create-mode drawer gating (PR 3) ────────────────────────────────


@pytest.mark.asyncio
class TestCreateModeDrawer:
    """The AI assistant drawer must be available on a brand-new composed
    slide (``/assets/new/composed``), not just after the first save. The
    drawer mints the draft on the first message client-side, so the
    server only needs to render it (with an empty asset id) whenever the
    Assistant feature flag is on for the user."""

    async def test_new_page_shows_drawer_for_enabled_user(self, client):
        # ``client`` is an admin (settings:write), so the assistant flag is
        # always on regardless of the allowlist.
        resp = await client.get("/assets/new/composed")
        assert resp.status_code == 200
        text = resp.text
        assert 'id="cw-ai"' in text
        assert '<script src="/static/composed_editor_chat.js"' in text
        # Create mode has no asset yet — the drawer must render an EMPTY
        # data-asset-id (not the string "None"), so the client treats it as
        # unbound and mints on first send.
        assert 'data-asset-id=""' in text
        assert 'data-asset-id="None"' not in text

    async def test_new_page_hides_drawer_for_disabled_user(
        self, operator_client, app
    ):
        # A non-admin Operator with the allowlist cleared has the assistant
        # disabled, so the drawer (and its scripts) must not render. The
        # Operator still has ASSETS_WRITE, so the page itself loads.
        await _disable_all(app)
        resp = await operator_client.get("/assets/new/composed")
        assert resp.status_code == 200
        text = resp.text
        assert 'id="cw-ai"' not in text
        assert '<script src="/static/composed_editor_chat.js"' not in text
