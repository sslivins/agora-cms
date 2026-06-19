"""Tests for the slideshow-editor embedded AI assistant (PR 1).

Mirrors ``tests/test_composed_editor_assistant.py`` for the slideshow
builder.  Covers the server-side pieces that back the chat panel
embedded in the slideshow editor:

* ``POST /api/assets/{id}/assistant/thread`` — get-or-create the
  editor-scoped, asset-bound chat thread (slideshow mode).
* ``list_threads`` excludes ``slideshow_editor`` threads from the
  general assistant sidebar.
* ``build_system_prompt`` renders the slideshow-editor variant only in
  ``slideshow_editor`` mode with a bound asset id (never widens).
* The slideshow builder template renders the shared assistant drawer
  in BOTH edit and create mode when the feature flag is on (create mode
  mints a draft slideshow on the first message, mirroring the composed
  editor); it's hidden only when the feature flag is off.
"""

from __future__ import annotations

import types
import uuid

import pytest
from sqlalchemy import select

from cms.models.asset import Asset, AssetType
from cms.models.chat_thread import ChatThread
from cms.models.slideshow_slide import SlideshowSlide
from cms.services.assistant.mcp_client import MODE_SLIDESHOW_EDITOR


# ── fixtures / helpers ──────────────────────────────────────────────


async def _make_slideshow(
    db_session, *, owner_id=None, is_global: bool = True, slides: int = 0,
) -> Asset:
    asset = Asset(
        filename=f"show-{uuid.uuid4().hex[:8]}",
        display_name="Test show",
        asset_type=AssetType.SLIDESHOW,
        size_bytes=0,
        checksum="v1",
        duration_seconds=10.0,
        uploaded_by_user_id=owner_id,
        is_global=is_global,
    )
    db_session.add(asset)
    await db_session.flush()
    if slides:
        src = Asset(
            filename=f"src-{uuid.uuid4().hex[:6]}.png",
            asset_type=AssetType.IMAGE,
            size_bytes=100,
            is_global=True,
        )
        db_session.add(src)
        await db_session.flush()
        for i in range(slides):
            db_session.add(SlideshowSlide(
                slideshow_asset_id=asset.id,
                source_asset_id=src.id,
                position=i,
                duration_ms=5000,
                play_to_end=False,
            ))
    await db_session.commit()
    return asset


async def _make_image(db_session, *, is_global: bool = True) -> Asset:
    asset = Asset(
        filename=f"img-{uuid.uuid4().hex[:8]}.png",
        asset_type=AssetType.IMAGE,
        size_bytes=100,
        checksum="v1",
        is_global=is_global,
    )
    db_session.add(asset)
    await db_session.commit()
    return asset


async def _disable_all(app) -> None:
    from cms.database import get_db
    from cms.services.assistant_flag import set_allowlist

    factory = app.dependency_overrides[get_db]
    async for db in factory():
        await set_allowlist(db, [])
        break


# ── POST /api/assets/{id}/assistant/thread ──────────────────────────


@pytest.mark.asyncio
class TestAssistantThreadEndpoint:
    async def test_creates_then_reuses_same_thread(self, client, db_session):
        asset = await _make_slideshow(db_session)

        first = await client.post(f"/api/assets/{asset.id}/assistant/thread")
        assert first.status_code == 200, first.text
        b1 = first.json()
        assert b1["created"] is True
        uuid.UUID(b1["thread_id"])

        second = await client.post(f"/api/assets/{asset.id}/assistant/thread")
        assert second.status_code == 200, second.text
        b2 = second.json()
        assert b2["created"] is False
        assert b2["thread_id"] == b1["thread_id"]

    async def test_thread_is_bound_and_in_editor_mode(self, client, db_session):
        asset = await _make_slideshow(db_session)
        resp = await client.post(f"/api/assets/{asset.id}/assistant/thread")
        assert resp.status_code == 200, resp.text
        tid = uuid.UUID(resp.json()["thread_id"])

        row = (
            await db_session.execute(
                select(ChatThread).where(ChatThread.id == tid)
            )
        ).scalar_one()
        assert row.mode == MODE_SLIDESHOW_EDITOR
        assert row.composed_asset_id == asset.id

    async def test_missing_asset_is_404(self, client):
        resp = await client.post(
            f"/api/assets/{uuid.uuid4()}/assistant/thread"
        )
        assert resp.status_code == 404

    async def test_non_slideshow_asset_is_400(self, client, db_session):
        img = await _make_image(db_session)
        resp = await client.post(f"/api/assets/{img.id}/assistant/thread")
        assert resp.status_code == 400, resp.text

    async def test_feature_off_is_404(self, operator_client, app, db_session):
        # Operator owns the slideshow (so it's visible), but the Assistant
        # feature is off → the endpoint must 404 to keep it invisible.
        await _disable_all(app)
        asset = await _make_slideshow(
            db_session, owner_id=operator_client.user_id, is_global=False,
        )
        resp = await operator_client.post(
            f"/api/assets/{asset.id}/assistant/thread"
        )
        assert resp.status_code == 404

    async def test_unauth_is_401(self, unauthed_client, db_session):
        asset = await _make_slideshow(db_session)
        resp = await unauthed_client.post(
            f"/api/assets/{asset.id}/assistant/thread"
        )
        assert resp.status_code in (401, 403)


# ── list_threads excludes editor threads ────────────────────────────


@pytest.mark.asyncio
class TestListThreadsExcludesEditor:
    async def test_editor_thread_hidden_from_sidebar(self, client, db_session):
        general = await client.post(
            "/api/chat/threads", json={"title": "Promo"}
        )
        assert general.status_code == 201
        general_id = general.json()["id"]

        asset = await _make_slideshow(db_session)
        editor = await client.post(f"/api/assets/{asset.id}/assistant/thread")
        editor_id = editor.json()["thread_id"]

        listing = (await client.get("/api/chat/threads")).json()
        ids = {t["id"] for t in listing}
        assert general_id in ids
        assert editor_id not in ids


# ── build_system_prompt mode awareness ──────────────────────────────


class TestBuildSystemPrompt:
    def _user(self):
        return types.SimpleNamespace(username="alice", email="alice@test.com")

    def test_slideshow_mode_renders_editor_prompt_with_bound_id(self):
        from cms.services.assistant.prompts import build_system_prompt

        aid = str(uuid.uuid4())
        prompt = build_system_prompt(
            self._user(), mode="slideshow_editor", composed_asset_id=aid
        )
        assert aid in prompt
        assert "Slideshow Assistant" in prompt
        assert "set_slideshow_slides" in prompt
        # Editor prompt must NOT advertise fleet tooling.
        assert "create_schedule" not in prompt

    def test_editor_prompt_documents_loop_transition(self):
        """The first slide's transition is the loop (last → first)
        transition; the assistant must be told so it can configure wraps."""
        from cms.services.assistant.prompts import build_system_prompt

        aid = str(uuid.uuid4())
        prompt = build_system_prompt(
            self._user(), mode="slideshow_editor", composed_asset_id=aid
        )
        assert "loop (last \u2192 first) transition" in prompt

    def test_editor_prompt_clarifies_ambiguous_fade(self):
        """Several transitions are fade-style (fade/fade_black/dissolve);
        when the operator asks for "a fade" the assistant must ask which
        one rather than guessing."""
        from cms.services.assistant.prompts import build_system_prompt

        aid = str(uuid.uuid4())
        prompt = build_system_prompt(
            self._user(), mode="slideshow_editor", composed_asset_id=aid
        )
        # Each fade-family transition is described so the assistant can
        # distinguish them, and it is told to ask when the request is
        # ambiguous.
        assert "fade through black" in prompt
        assert "crossfade" in prompt
        assert "ask them which one" in prompt

    def test_editor_prompt_documents_fit_and_effect(self):
        """The per-slide ``fit`` (incl. blur-fill) and ``effect`` (Ken
        Burns) options must be advertised so the assistant can set them."""
        from cms.services.assistant.prompts import build_system_prompt

        aid = str(uuid.uuid4())
        prompt = build_system_prompt(
            self._user(), mode="slideshow_editor", composed_asset_id=aid
        )
        # fit values + the blur-fill option are described.
        assert "contain_blur" in prompt
        assert "cover" in prompt
        # effect values incl. Ken Burns are described.
        assert "ken_burns" in prompt

    def test_editor_prompt_documents_tag_block_styling(self):
        """The tag-block section must teach the assistant that it can
        style/time blocks (incl. member_transition + effect_direction),
        that members are dynamic (no per-member styling), and that
        membership/tag-creation is NOT done from the editor."""
        from cms.services.assistant.prompts import build_system_prompt

        aid = str(uuid.uuid4())
        prompt = build_system_prompt(
            self._user(), mode="slideshow_editor", composed_asset_id=aid
        )
        # Tag-block playback controls are advertised.
        assert "tag block" in prompt
        assert "member_transition" in prompt
        assert "effect_direction" in prompt
        # Resolving a tag name to its id is via list_tags.
        assert "list_tags" in prompt
        # Members are dynamic — the assistant must NOT think it can style
        # them one at a time.
        assert "dynamic" in prompt.lower()
        # Membership / tag creation is library-side; the editor prompt must
        # NOT promise membership/creation tools it cannot call in this mode.
        assert "tag_asset" not in prompt
        assert "untag_asset" not in prompt
        assert "create_tag" not in prompt
        assert "library" in prompt.lower()

    def test_slideshow_mode_without_id_falls_back_to_general(self):
        from cms.services.assistant.prompts import build_system_prompt

        prompt = build_system_prompt(
            self._user(), mode="slideshow_editor", composed_asset_id=None
        )
        assert "Slideshow Assistant" not in prompt

    def test_default_mode_is_general(self):
        from cms.services.assistant.prompts import build_system_prompt

        prompt = build_system_prompt(self._user())
        assert "Slideshow Assistant" not in prompt


class TestSlideshowEditorToolProfile:
    """The slideshow-editor mode exposes exactly the tools needed to
    discover assets/tags and read+write the open slideshow — and nothing
    that would let it mutate other assets, tag membership, or global tags.
    """

    def test_list_tags_is_in_editor_profile(self):
        """``list_tags`` (read) lets the assistant resolve a tag name to
        the ``tag_id`` a dynamic tag block requires."""
        from cms.services.assistant.mcp_client import (
            MODE_SLIDESHOW_EDITOR,
            tools_for_mode,
        )

        tools = tools_for_mode(MODE_SLIDESHOW_EDITOR)
        assert "list_tags" in tools
        assert "set_slideshow_slides" in tools
        assert "get_slideshow" in tools

    def test_membership_and_tag_creation_excluded_from_editor(self):
        """Editor mode must stay tight: no tag-membership writes (which
        touch OTHER assets) and no global tag CRUD."""
        from cms.services.assistant.mcp_client import (
            MODE_SLIDESHOW_EDITOR,
            tools_for_mode,
        )

        tools = tools_for_mode(MODE_SLIDESHOW_EDITOR)
        for forbidden in ("tag_asset", "untag_asset", "create_tag", "delete_tag"):
            assert forbidden not in tools


# ── builder template drawer gating (feature-flag-gated, both modes) ──


@pytest.mark.asyncio
class TestBuilderDrawerGating:
    async def test_edit_page_shows_drawer_for_enabled_user(
        self, client, db_session
    ):
        # ``client`` is an admin (settings:write), so the assistant flag is
        # always on regardless of the allowlist.
        asset = await _make_slideshow(db_session, slides=1)
        resp = await client.get(f"/assets/{asset.id}/slideshow")
        assert resp.status_code == 200, resp.text
        text = resp.text
        assert 'id="cw-ai"' in text
        assert '<script src="/static/editor_chat.js"' in text
        assert f'data-asset-id="{asset.id}"' in text

    async def test_create_page_shows_drawer(self, client):
        # The assistant is available from the very first moment, including
        # create mode: a brand-new slideshow has no bound asset, so the
        # drawer mints a draft on the first message (window.slideshowMintDraft),
        # mirroring the composed editor. The drawer + its scripts must render
        # for an admin with the feature on, and the create-mode mint must be
        # wired into cwAiConfig (not disabled).
        resp = await client.get("/assets/new/slideshow")
        assert resp.status_code == 200, resp.text
        text = resp.text
        assert 'id="cw-ai"' in text
        assert '<script src="/static/editor_chat.js"' in text
        # Create-mode mint must be wired in (NOT mint: null).
        assert "window.slideshowMintDraft" in text
        assert "mint: window.slideshowMintDraft" in text
        assert "mint: null" not in text

    async def test_edit_page_hides_drawer_for_disabled_user(
        self, operator_client, app, db_session
    ):
        await _disable_all(app)
        asset = await _make_slideshow(
            db_session, owner_id=operator_client.user_id, is_global=False,
        )
        resp = await operator_client.get(f"/assets/{asset.id}/slideshow")
        assert resp.status_code == 200, resp.text
        text = resp.text
        assert 'id="cw-ai"' not in text
        assert '<script src="/static/editor_chat.js"' not in text
