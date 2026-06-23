"""Smoke tests for slideshow builder UI routes (Commit 4)."""

from __future__ import annotations

import uuid
from datetime import date, time

import pytest

from cms.models.asset import Asset, AssetType
from cms.models.slideshow_slide import SlideshowSlide
from cms.models.tag import AssetTag, Tag
from cms.models.user import User


pytestmark = pytest.mark.asyncio


async def _seed_slideshow(
    db_session, *, name="My Show", is_global=True, slides=0,
    slide_fit="cover", slide_effect="none", slide_window=None,
):
    asset = Asset(
        filename=name,
        asset_type=AssetType.SLIDESHOW,
        size_bytes=0,
        checksum="v1",
        duration_seconds=10.0,
        is_global=is_global,
    )
    db_session.add(asset)
    await db_session.flush()
    if slides:
        # Need a real source asset to FK against
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
                fit=slide_fit,
                effect=slide_effect,
                **(slide_window or {}),
            ))
    await db_session.commit()
    return asset


class TestSlideshowBuilderRoutes:

    async def test_new_page_renders(self, client):
        resp = await client.get("/assets/new/slideshow")
        assert resp.status_code == 200, resp.text
        body = resp.text
        assert "New Slideshow" in body
        assert "ss-slides-table" in body
        assert "/api/assets/slideshow" in body  # JS POST endpoint baked in

    async def test_builder_supports_composed_members(self, client):
        """Builder must offer composed slides in the asset library
        (Phase 5 composed-in-slideshow)."""
        resp = await client.get("/assets/new/slideshow")
        assert resp.status_code == 200, resp.text
        body = resp.text
        # Library filter + fetch pull composed assets alongside image/video.
        assert "'image', 'video', 'composed'" in body

    async def test_new_page_offers_blur_fill_fit_option(self, client):
        """Blur-fill fit (agora#261) must be selectable in the builder."""
        resp = await client.get("/assets/new/slideshow")
        assert resp.status_code == 200, resp.text
        assert 'value="contain_blur"' in resp.text

    async def test_blur_fill_slot_preview_is_wysiwyg(self, client):
        """The slot thumbnail must render a blurred cover backdrop for
        ``contain_blur`` so the letterbox bars match the device player
        instead of showing plain black bars."""
        resp = await client.get("/assets/new/slideshow")
        assert resp.status_code == 200, resp.text
        # CSS for the blur backdrop + foreground layers.
        assert "ssb-slot-thumb-backdrop" in resp.text
        assert "ssb-slot-thumb-fg" in resp.text
        assert "filter: blur(" in resp.text
        # makeSlot() gates the backdrop on contain_blur images only.
        assert "s.fit === 'contain_blur' && !isVideo" in resp.text
        # The slide-number / remove / move overlays must sit above the
        # blur foreground (z-index 1) so they aren't hidden under it.
        for cls in (".ssb-slot-pos", ".ssb-slot-remove", ".ssb-slot-move"):
            start = resp.text.index(cls + " {")
            block = resp.text[start:start + 400]
            assert "z-index: 2" in block, f"{cls} missing z-index above blur fg"

    async def test_visibility_block_has_quick_fill_chips(self, client):
        """The per-slide Visibility block offers scheduler-style focus-reveal
        quick-fill popup chips: a "Now" chip for the time-of-day Start and a
        "Today" chip for the date-range Start, each wired to a handler. The
        chips reuse the global ``.time-now-wrap``/``.time-now-chip`` pattern
        and appear only while the Start input (or chip) is focused — they are
        NOT permanent inline buttons."""
        resp = await client.get("/assets/new/slideshow")
        assert resp.status_code == 200, resp.text
        # Chip buttons in visibilityCtlHtml.
        assert "ssb-vis-now-btn" in resp.text
        assert "ssb-vis-today-btn" in resp.text
        # Popup-chip pattern: reuse the scheduler chip classes, not permanent
        # inline buttons.
        assert "time-now-chip" in resp.text
        assert "time-now-wrap" in resp.text
        # The old permanent-button markup must be gone.
        assert "ssb-vis-nowbtn" not in resp.text
        # Focus-reveal wiring via the shared chip helper + .focused class.
        assert "wireVisChip(" in resp.text
        assert "classList.add('focused')" in resp.text
        # Click handlers wired in wireVisibilityCtls.
        assert "setSlideVisNow(i)" in resp.text
        assert "setSlideVisToday(i)" in resp.text
        # "Set to now" seeds End +1h ONLY when End is empty (open-ended
        # windows typed by hand stay open).
        assert "if (!s.active_end)" in resp.text

    async def test_create_mode_preview_btn_hidden_until_mint(self, client):
        """Bug: AI-assistant slideshow create left the page in create-mode
        chrome — Create button never flipped to Save and no Preview button
        appeared. The Preview button must be rendered (hidden) in create
        mode, and slideshowMintDraft must reveal it + relabel submit via
        applyEditModeChrome()."""
        resp = await client.get("/assets/new/slideshow")
        assert resp.status_code == 200, resp.text
        body = resp.text
        # Preview button is present in create mode but hidden, so JS can
        # reveal it after minting (rather than not existing at all).
        assert 'id="ss-preview-btn"' in body
        assert 'title="Preview the saved slideshow" hidden' in body
        assert "applyEditModeChrome" in body
        # The mint path flips the chrome to edit mode.
        assert "applyEditModeChrome();" in body

    async def test_new_page_requires_write_permission(self, app, db_session):
        """Direct nav to /assets/new/slideshow must be gated on assets:write."""
        from tests.test_ui_overhaul import _create_user, _login_as
        await _create_user(db_session, username="ss_viewer", role_name="Viewer")
        ac = await _login_as(app, "ss_viewer")
        try:
            resp = await ac.get("/assets/new/slideshow")
            assert resp.status_code == 403, resp.text
        finally:
            await ac.aclose()

    async def test_hub_renders_for_writer(self, client):
        resp = await client.get("/assets/new")
        assert resp.status_code == 200, resp.text
        body = resp.text
        # Slideshow tile is the only builder today; must link to the
        # existing builder route.
        assert 'href="/assets/new/slideshow"' in body
        assert "Slideshow" in body

    async def test_hub_requires_write_permission(self, app, db_session):
        from tests.test_ui_overhaul import _create_user, _login_as
        await _create_user(db_session, username="hub_viewer", role_name="Viewer")
        ac = await _login_as(app, "hub_viewer")
        try:
            resp = await ac.get("/assets/new")
            assert resp.status_code == 403, resp.text
        finally:
            await ac.aclose()

    async def test_edit_page_renders_with_existing_slides(self, client, db_session):
        asset = await _seed_slideshow(db_session, name="Editable", slides=3)
        resp = await client.get(f"/assets/{asset.id}/slideshow")
        assert resp.status_code == 200, resp.text
        body = resp.text
        assert "Edit Slideshow" in body
        assert "Editable" in body
        # Ensure the seeded slides are in the JSON island the page uses for state
        assert '"position": 0' in body or '"position":0' in body

    async def test_edit_page_hydrates_fit_and_effect(self, client, db_session):
        """Regression: per-slide fit/effect must survive the save -> reopen
        round-trip. The edit-page JSON island (ss-initial-slides) is the
        hydration source; if fit/effect are dropped here the editor silently
        reverts them to defaults (cover/none) on reload."""
        asset = await _seed_slideshow(
            db_session, name="FitFx", slides=1,
            slide_fit="contain", slide_effect="ken_burns",
        )
        resp = await client.get(f"/assets/{asset.id}/slideshow")
        assert resp.status_code == 200, resp.text
        body = resp.text
        assert '"fit": "contain"' in body or '"fit":"contain"' in body
        assert '"effect": "ken_burns"' in body or '"effect":"ken_burns"' in body

    async def test_edit_page_hydrates_visibility_windows(self, client, db_session):
        """Regression: per-slide visibility windows must survive the
        save -> reopen round-trip. The edit-page JSON island
        (ss-initial-slides) is the hydration source; if the window
        columns are dropped here the editor shows blank visibility on
        reload even though the rule is still applied server-side."""
        asset = await _seed_slideshow(
            db_session, name="VisShow", slides=1,
            slide_window={
                "valid_from": date(2026, 12, 1),
                "valid_to": date(2026, 12, 26),
                "active_start": time(13, 0),
                "active_end": time(14, 0),
                "active_days": [0, 1, 2, 3, 4],
            },
        )
        resp = await client.get(f"/assets/{asset.id}/slideshow")
        assert resp.status_code == 200, resp.text
        body = resp.text
        assert "2026-12-01" in body
        assert "2026-12-26" in body
        assert "13:00:00" in body
        assert "14:00:00" in body
        assert '"active_days": [0, 1, 2, 3, 4]' in body or '"active_days":[0,1,2,3,4]' in body

    async def test_edit_page_wires_in_editor_rename(self, client, db_session):
        """Edit mode must persist a renamed slideshow in-editor. The /slides
        PUT can't carry a name, so a changed name is PATCHed to
        /api/assets/{id} {display_name}. Regression for the silent
        edit-mode rename drop."""
        asset = await _seed_slideshow(db_session, name="Renameable", slides=2)
        resp = await client.get(f"/assets/{asset.id}/slideshow")
        assert resp.status_code == 200, resp.text
        body = resp.text
        # Rename baseline + helper are baked into the edit page.
        assert "SS_ORIG_NAME" in body
        assert "maybeRenameSlideshow" in body
        # Baseline is seeded with the current name so an unchanged name is a no-op.
        assert "Renameable" in body
        # The old "rename is managed via the Assets page" note must be gone.
        assert "and rename are managed" not in body

    async def test_new_page_omits_editor_rename_baseline(self, client):
        """Create mode has no asset yet — the rename baseline is empty and
        the helper is still defined (the mint flow flips into edit mode)."""
        resp = await client.get("/assets/new/slideshow")
        assert resp.status_code == 200, resp.text
        body = resp.text
        assert 'SS_ORIG_NAME = ""' in body
        assert "maybeRenameSlideshow" in body

    async def test_edit_page_404s_for_non_slideshow(self, client, db_session):
        # Image, not slideshow: should redirect away
        img = Asset(
            filename="not-a-show.png",
            asset_type=AssetType.IMAGE,
            size_bytes=10,
            is_global=True,
        )
        db_session.add(img)
        await db_session.commit()
        resp = await client.get(f"/assets/{img.id}/slideshow", follow_redirects=False)
        assert resp.status_code in (303, 307, 302)

    async def test_edit_page_redirects_for_unknown_id(self, client):
        bogus = uuid.uuid4()
        resp = await client.get(f"/assets/{bogus}/slideshow", follow_redirects=False)
        assert resp.status_code in (303, 307, 302)

    async def test_assets_page_links_to_create_hub(self, client, db_session):
        resp = await client.get("/assets")
        assert resp.status_code == 200
        # Library page now links to the Create hub (sub-tab) rather than
        # to the slideshow builder directly.
        assert 'href="/assets/new"' in resp.text
        # The legacy "🎞️ New Slideshow" button on the Library card has
        # been retired in favour of the Create tab — make sure it is gone.
        assert "🎞️ New Slideshow" not in resp.text

    async def test_assets_page_shows_slide_count_badge(self, client, db_session):
        await _seed_slideshow(db_session, name="Count Show", slides=3)
        resp = await client.get("/assets")
        assert resp.status_code == 200
        # Badge text from _macros.html
        assert "3 slides" in resp.text


class TestSlideshowLoopTransitionUI:
    """The leading timeline gap doubles as the loop (last → first)
    transition control bound to slides[0]. Behavior is implemented in the
    baked builder JS, so assert the source ships and is gated correctly."""

    async def test_builder_bakes_loop_transition_control(self, client):
        resp = await client.get("/assets/new/slideshow")
        assert resp.status_code == 200, resp.text
        body = resp.text
        # Shared popover builder reused for both between-slide and loop gaps.
        assert "function buildTransitionGap(" in body
        # Distinct loop styling + accessible label.
        assert "ssb-gap-btn-loop" in body
        assert "loop (last \u2192 first)" in body

    async def test_loop_control_gated_on_two_or_more_slides(self, client):
        """The leading gap only becomes the loop control with >=2 slides;
        a 0–1 slide show never wraps with a visible transition."""
        resp = await client.get("/assets/new/slideshow")
        assert resp.status_code == 200, resp.text
        body = resp.text
        # The gating branch in makeGap's leading path.
        assert "slides.length >= 2" in body
        # Loop control binds to slides[0].
        assert "buildTransitionGap(gap, 0, /*isLoop*/ true)" in body


class TestSlideshowBuilderTagPalette:
    """Phase 2: the dynamic-tags palette lets users author live tag
    blocks visually (click/drag a tag chip onto the timeline)."""

    async def _seed_tag(self, db_session, *, name="Promos", color="#1f9d55", members=0):
        tag = Tag(name=name, color=color)
        db_session.add(tag)
        await db_session.flush()
        for _ in range(members):
            src = Asset(
                filename=f"src-{uuid.uuid4().hex[:6]}.png",
                asset_type=AssetType.IMAGE,
                size_bytes=100,
                is_global=True,
            )
            db_session.add(src)
            await db_session.flush()
            db_session.add(AssetTag(asset_id=src.id, tag_id=tag.id))
        await db_session.commit()
        return tag

    async def test_palette_renders_in_create_mode(self, client, db_session):
        """The palette markup + JS + the ss-all-tags JSON island must be
        baked into a fresh (create-mode) builder page."""
        await self._seed_tag(db_session, name="Promos", members=2)
        resp = await client.get("/assets/new/slideshow")
        assert resp.status_code == 200, resp.text
        body = resp.text
        # Palette container + section heading.
        assert 'id="ss-tags-palette"' in body
        assert "Dynamic tags" in body
        # JSON island feeding the palette + the JS that consumes it.
        assert 'id="ss-all-tags"' in body
        assert "renderTagPalette" in body
        assert "function addTagBlock(" in body

    async def test_all_tags_island_carries_member_count(self, client, db_session):
        """Each tag in the ss-all-tags island must include its playable
        member_count so the chip can show how many assets it resolves to
        and updateTotals can estimate run time."""
        await self._seed_tag(db_session, name="Featured", members=3)
        resp = await client.get("/assets/new/slideshow")
        assert resp.status_code == 200, resp.text
        body = resp.text
        assert "Featured" in body
        assert "member_count" in body

    async def test_tag_drop_branch_wired(self, client):
        """The timeline drop handler must dispatch the tag payload kind
        to addTagBlock (drag a chip onto the timeline)."""
        resp = await client.get("/assets/new/slideshow")
        assert resp.status_code == 200, resp.text
        body = resp.text
        assert "payload.kind === 'tag'" in body

    async def test_tag_block_exposes_fit_and_motion_controls(self, client):
        """A dynamic tag block must expose the same Fit + Motion controls
        as an asset slide. The chosen values become the deck-default every
        expanded member inherits (the write path already serializes
        fit/effect/effect_direction for kind='tag'). Regression for the
        builder gap where makeTagSlot rendered no fit/motion controls, so a
        tag block was silently locked to cover/none."""
        resp = await client.get("/assets/new/slideshow")
        assert resp.status_code == 200, resp.text
        body = resp.text
        # Shared helpers exist (asset + tag tiles render identical controls).
        assert "function fitEffectCtlHtml(" in body
        assert "function wireFitEffectCtls(" in body
        # makeTagSlot wires both the markup and the listeners.
        start = body.index("function makeTagSlot(")
        end = body.index("function ", start + 1)
        tag_fn = body[start:end]
        assert "fitEffectCtlHtml(s, i)" in tag_fn
        assert "wireFitEffectCtls(slot, i)" in tag_fn

    async def test_palette_renders_in_edit_mode(self, client, db_session):
        """The palette must also be present when editing a saved
        slideshow, not just on the create page."""
        await self._seed_tag(db_session, name="Promos", members=1)
        asset = await _seed_slideshow(db_session, name="Editable", slides=1)
        resp = await client.get(f"/assets/{asset.id}/slideshow")
        assert resp.status_code == 200, resp.text
        body = resp.text
        assert 'id="ss-tags-palette"' in body
        assert 'id="ss-all-tags"' in body


async def _seed_clipped_video_slideshow(db_session, *, start_ms, clip_ms):
    """Seed a slideshow with one VIDEO slide carrying a clip (trim) window."""
    show = Asset(
        filename="ClipShow",
        asset_type=AssetType.SLIDESHOW,
        size_bytes=0,
        checksum="v1",
        duration_seconds=10.0,
        is_global=True,
    )
    db_session.add(show)
    await db_session.flush()
    src = Asset(
        filename=f"clip-{uuid.uuid4().hex[:6]}.mp4",
        asset_type=AssetType.VIDEO,
        size_bytes=1000,
        duration_seconds=86.0,
        is_global=True,
    )
    db_session.add(src)
    await db_session.flush()
    db_session.add(SlideshowSlide(
        slideshow_asset_id=show.id,
        source_asset_id=src.id,
        position=0,
        duration_ms=clip_ms if clip_ms is not None else 5000,
        play_to_end=clip_ms is None,
        clip_start_ms=start_ms,
        clip_duration_ms=clip_ms,
    ))
    await db_session.commit()
    return show


class TestSlideshowBuilderTrimUI:
    """The per-slide video trim (clip) control."""

    async def test_builder_bakes_trim_helpers(self, client):
        """Trim render + wiring + the effective-playback helper must be
        baked into the builder JS."""
        resp = await client.get("/assets/new/slideshow")
        assert resp.status_code == 200, resp.text
        body = resp.text
        assert "function trimCtlHtml(" in body
        assert "function wireTrimCtls(" in body
        assert "function effectivePlaybackMs(" in body
        assert "function updateSlideClipStart(" in body
        # The trim block is only rendered for video slides.
        assert "if (s.source_asset_type !== 'video') return '';" in body
        # Trim block is appended into the slot meta + wired up.
        assert "${trimCtlHtml(s, i)}" in body
        assert "wireTrimCtls(slot, i);" in body

    async def test_builder_bakes_trim_preview_editor(self, client):
        """The visual "Preview & trim" modal (scrub bar + in/out handles +
        play-selection) must be baked into the builder JS and wired to a
        button in the trim section."""
        resp = await client.get("/assets/new/slideshow")
        assert resp.status_code == 200, resp.text
        body = resp.text
        # Entry-point button rendered in trimCtlHtml + wired in wireTrimCtls.
        assert "ssb-trim-preview-btn" in body
        assert "openTrimEditor(i)" in body
        # The modal builder + write-back helpers exist.
        assert "function openTrimEditor(" in body
        assert "function applyTrim(" in body
        # Reuses the shared lightbox chrome, not a bespoke modal.
        assert "createModal(" in body
        # Two-handle scrub bar + play-selection loop.
        assert "ssb-trim-in" in body
        assert "ssb-trim-out" in body
        assert "Play selection" in body
        # Write-back keeps the existing clip grammar: out-point at the source
        # end is play-to-end, an earlier out-point pins a fixed length.
        assert "s.clip_start_ms = inMs;" in body
        assert "s.duration_ms = outMs - inMs;" in body

    async def test_serialize_uses_byte_identical_clip_rule(self, client):
        """clip_duration_ms must stay null unless a video has a real start
        offset AND play-to-end is off, so un-trimmed decks serialize
        byte-identically (clip_start_ms:0 / clip_duration_ms:null)."""
        resp = await client.get("/assets/new/slideshow")
        body = resp.text
        # The serialize rule predicate.
        assert "(isVideoSlide && !s.play_to_end && clipStartMs > 0)" in body
        # clip fields are emitted on the asset wire shape.
        assert "clip_start_ms: clipStartMs," in body
        assert "clip_duration_ms: clipDurationMs," in body

    async def test_new_slides_init_clip_defaults(self, client):
        """Fresh slides (single + bulk add) start un-trimmed."""
        resp = await client.get("/assets/new/slideshow")
        body = resp.text
        assert body.count("clip_start_ms: 0,") >= 2
        assert body.count("clip_duration_ms: null,") >= 2

    async def test_edit_page_hydrates_clip_fields(self, client, db_session):
        """Regression: a clipped video slide must round-trip its clip
        window through the ss-initial-slides hydration island, otherwise
        the trim UI shows 'Full' on reload even though the server still
        clips the playback."""
        asset = await _seed_clipped_video_slideshow(
            db_session, start_ms=5000, clip_ms=8000,
        )
        resp = await client.get(f"/assets/{asset.id}/slideshow")
        assert resp.status_code == 200, resp.text
        body = resp.text
        assert '"clip_start_ms": 5000' in body or '"clip_start_ms":5000' in body
        assert '"clip_duration_ms": 8000' in body or '"clip_duration_ms":8000' in body

    async def test_edit_page_hydrates_play_to_end_clip(self, client, db_session):
        """A clip-from-offset-to-end slide (play_to_end, start>0, no fixed
        length) hydrates with clip_start_ms set and clip_duration_ms null."""
        asset = await _seed_clipped_video_slideshow(
            db_session, start_ms=5000, clip_ms=None,
        )
        resp = await client.get(f"/assets/{asset.id}/slideshow")
        assert resp.status_code == 200, resp.text
        body = resp.text
        assert '"clip_start_ms": 5000' in body or '"clip_start_ms":5000' in body
        assert '"clip_duration_ms": null' in body or '"clip_duration_ms":null' in body
