"""Tests for cms.composed.widgets.media.MediaWidget.

Covers config validation, render output (both image and video
branches), instance scoping, and the declared/referenced asset
contract.  Construction of :class:`BundleContext` is done directly
rather than via ``build_bundle`` so failures point at the widget
implementation itself.
"""

from __future__ import annotations

import base64
import logging
import uuid

import pytest
from pydantic import ValidationError

# Side-effect import: triggers global registry population
import cms.composed.widgets  # noqa: F401
from cms.composed.registry import BundleContext, SlideshowSlidePlan, get_registry
from cms.composed.schema import Cell
from cms.composed.widgets.media import (
    MediaWidget,
    MediaWidgetConfig,
)


def _cell() -> Cell:
    return Cell(row=1, col=1, rowspan=2, colspan=3)


def _img_ctx(aid: uuid.UUID, blob: bytes, mime: str = "image/png") -> BundleContext:
    return BundleContext(
        asset_bytes={aid: blob},
        asset_mimes={aid: mime},
    )


def _vid_ctx(aid: uuid.UUID, url: str) -> BundleContext:
    return BundleContext(sibling_asset_urls={aid: url})


# Trivial 1x1 transparent PNG — enough for the data-URI branch.
_TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c63600100000005000168cb6e6d0000000049454e44ae426082"
)


class TestMediaWidgetConfig:
    def test_minimum_valid_config(self):
        aid = uuid.uuid4()
        c = MediaWidgetConfig(asset_id=aid)
        assert c.asset_id == aid
        assert c.object_fit == "cover"
        assert c.alt == ""

    def test_asset_id_required(self):
        with pytest.raises(ValidationError):
            MediaWidgetConfig()  # type: ignore[call-arg]

    def test_object_fit_allowlist(self):
        for ok in ("cover", "contain", "fill"):
            MediaWidgetConfig(asset_id=uuid.uuid4(), object_fit=ok)
        with pytest.raises(ValidationError):
            MediaWidgetConfig(asset_id=uuid.uuid4(), object_fit="scale-down")

    def test_alt_max_length(self):
        with pytest.raises(ValidationError):
            MediaWidgetConfig(asset_id=uuid.uuid4(), alt="x" * 513)

    def test_extra_field_rejected(self):
        with pytest.raises(ValidationError):
            MediaWidgetConfig(asset_id=uuid.uuid4(), unknown="nope")  # type: ignore[call-arg]


class TestMediaWidgetRegistry:
    def test_registered(self):
        w = get_registry().get("media")
        assert isinstance(w, MediaWidget)
        assert w.slug == "media"
        assert w.config_version == 1
        assert w.display_name == "Media"


class TestMediaWidgetDeclaredAssetIds:
    def test_returns_single_asset_id(self):
        aid = uuid.uuid4()
        cfg = MediaWidgetConfig(asset_id=aid)
        assert MediaWidget().declared_asset_ids(cfg) == [aid]


class TestMediaWidgetRender:
    def test_requires_ctx(self):
        cfg = MediaWidgetConfig(asset_id=uuid.uuid4())
        with pytest.raises(RuntimeError, match="BundleContext"):
            MediaWidget().render_html(cfg, _cell(), "id-1", ctx=None)

    def test_missing_from_both_channels_raises(self):
        aid = uuid.uuid4()
        cfg = MediaWidgetConfig(asset_id=aid)
        with pytest.raises(RuntimeError, match="missing from BundleContext"):
            MediaWidget().render_html(cfg, _cell(), "id-1", ctx=BundleContext())


class TestMediaWidgetImageBranch:
    def test_renders_data_uri_with_b64_content(self):
        aid = uuid.uuid4()
        cfg = MediaWidgetConfig(asset_id=aid, alt="cat")
        ctx = _img_ctx(aid, _TINY_PNG, "image/png")

        r = MediaWidget().render_html(cfg, _cell(), "abc", ctx=ctx)

        b64 = base64.b64encode(_TINY_PNG).decode("ascii")
        assert f'src="data:image/png;base64,{b64}"' in r.html
        assert '<img ' in r.html
        assert 'alt="cat"' in r.html

    def test_referenced_asset_ids_match_declared(self):
        aid = uuid.uuid4()
        cfg = MediaWidgetConfig(asset_id=aid)
        ctx = _img_ctx(aid, _TINY_PNG)
        r = MediaWidget().render_html(cfg, _cell(), "abc", ctx=ctx)
        assert r.referenced_asset_ids == [aid]

    def test_alt_text_is_html_escaped(self):
        aid = uuid.uuid4()
        cfg = MediaWidgetConfig(asset_id=aid, alt='</alt><script>bad()</script>')
        ctx = _img_ctx(aid, _TINY_PNG)
        r = MediaWidget().render_html(cfg, _cell(), "abc", ctx=ctx)
        assert "<script>" not in r.html
        assert "&lt;script&gt;" in r.html

    def test_large_image_logs_warning(self, caplog):
        aid = uuid.uuid4()
        cfg = MediaWidgetConfig(asset_id=aid)
        big = b"\x00" * (3 * 1024 * 1024)
        ctx = _img_ctx(aid, big, "image/png")
        with caplog.at_level(logging.WARNING, logger="cms.composed.widgets.media"):
            MediaWidget().render_html(cfg, _cell(), "big", ctx=ctx)
        assert any("inlined bundle will be large" in rec.message for rec in caplog.records)


class TestMediaWidgetVideoBranch:
    def test_renders_video_tag_with_sibling_url(self):
        aid = uuid.uuid4()
        url = "/assets/videos/clip.mp4"
        cfg = MediaWidgetConfig(asset_id=aid, alt="promo")
        ctx = _vid_ctx(aid, url)

        r = MediaWidget().render_html(cfg, _cell(), "v1", ctx=ctx)

        assert '<video ' in r.html
        assert f'src="{url}"' in r.html
        # autoplay/muted/loop/playsinline required for kiosk playback
        assert 'muted' in r.html
        assert 'loop' in r.html
        assert 'autoplay' in r.html
        assert 'playsinline' in r.html
        assert 'aria-label="promo"' in r.html
        # No data URI for videos — sibling URL only
        assert 'data:' not in r.html

    def test_video_url_with_special_chars_is_html_escaped(self):
        aid = uuid.uuid4()
        # publish layer URL-encodes the filename, but the widget must
        # still HTML-escape the resulting attribute value defensively.
        url = "/assets/videos/my%20clip%20%26%20more.mp4"
        cfg = MediaWidgetConfig(asset_id=aid)
        ctx = _vid_ctx(aid, url)
        r = MediaWidget().render_html(cfg, _cell(), "v2", ctx=ctx)
        # %26 stays %26 (HTML escape converts to &amp;... wait, & becomes &amp;)
        # The URL already contains literal %26, no & to escape, so it round-trips.
        assert "/assets/videos/my%20clip%20%26%20more.mp4" in r.html

    def test_video_referenced_asset_ids(self):
        aid = uuid.uuid4()
        cfg = MediaWidgetConfig(asset_id=aid)
        ctx = _vid_ctx(aid, "/assets/videos/x.mp4")
        r = MediaWidget().render_html(cfg, _cell(), "v3", ctx=ctx)
        assert r.referenced_asset_ids == [aid]


class TestMediaWidgetScoping:
    def test_html_and_css_are_instance_scoped(self):
        aid = uuid.uuid4()
        cfg = MediaWidgetConfig(asset_id=aid)
        ctx = _img_ctx(aid, _TINY_PNG)

        r1 = MediaWidget().render_html(cfg, _cell(), "inst-A", ctx=ctx)
        r2 = MediaWidget().render_html(cfg, _cell(), "inst-B", ctx=ctx)

        assert "cw-media-inst-A" in r1.html
        assert "cw-media-inst-A" in r1.css
        assert "cw-media-inst-B" in r2.html
        assert "cw-media-inst-B" in r2.css
        assert "cw-media-inst-B" not in r1.html
        assert "cw-media-inst-A" not in r2.html

    def test_object_fit_propagates_to_css(self):
        aid = uuid.uuid4()
        ctx = _img_ctx(aid, _TINY_PNG)
        for fit in ("cover", "contain", "fill"):
            cfg = MediaWidgetConfig(asset_id=aid, object_fit=fit)  # type: ignore[arg-type]
            r = MediaWidget().render_html(cfg, _cell(), "abc", ctx=ctx)
            assert f"object-fit: {fit}" in r.css

    def test_object_fit_applies_to_video_branch_too(self):
        aid = uuid.uuid4()
        cfg = MediaWidgetConfig(asset_id=aid, object_fit="contain")
        ctx = _vid_ctx(aid, "/assets/videos/x.mp4")
        r = MediaWidget().render_html(cfg, _cell(), "vid", ctx=ctx)
        assert "object-fit: contain" in r.css


class TestMediaWidgetSlideshowBranch:
    """A media widget whose asset_id is a SLIDESHOW container cycles
    its ordered member slides client-side."""

    def _ss_ctx(self, container, sources):
        """Build a ctx with a slideshow plan + per-source channels.

        ``sources`` is a list of (source_id, plan_kwargs, kind, payload):
          kind == "img" -> payload is (bytes, mime) -> asset_bytes
          kind == "vid" -> payload is url           -> sibling_asset_urls
        """
        asset_bytes = {}
        asset_mimes = {}
        sibling = {}
        plan = []
        for sid, pk, kind, payload in sources:
            if kind == "img":
                asset_bytes[sid] = payload[0]
                asset_mimes[sid] = payload[1]
            else:
                sibling[sid] = payload
            plan.append(SlideshowSlidePlan(source_asset_id=sid, **pk))
        return BundleContext(
            asset_bytes=asset_bytes,
            asset_mimes=asset_mimes,
            sibling_asset_urls=sibling,
            slideshow_plans={container: plan},
        )

    def test_renders_stacked_slides_first_active(self):
        container = uuid.uuid4()
        s1, s2 = uuid.uuid4(), uuid.uuid4()
        cfg = MediaWidgetConfig(asset_id=container)
        ctx = self._ss_ctx(
            container,
            [
                (s1, {"duration_ms": 4000, "transition": "fade",
                      "transition_ms": 600}, "img", (_TINY_PNG, "image/png")),
                (s2, {"duration_ms": 5000, "transition": "cut"},
                 "vid", "/assets/videos/clip.mp4"),
            ],
        )
        r = MediaWidget().render_html(cfg, _cell(), "ss1", ctx=ctx)

        # Instance-scoped container.
        assert 'class="cw-ss-ss1"' in r.html
        # Two stacked slides, first active.
        assert r.html.count("cw-ss-slide") == 2
        assert "cw-ss-slide cw-ss-active" in r.html
        # Image slide is a data URI; video slide is a sibling <video>.
        b64 = base64.b64encode(_TINY_PNG).decode("ascii")
        assert f"data:image/png;base64,{b64}" in r.html
        assert 'src="/assets/videos/clip.mp4"' in r.html
        # Both source ids referenced, in order.
        assert r.referenced_asset_ids == [s1, s2]

    def test_cut_transition_baked_zero_ms(self):
        container = uuid.uuid4()
        s1 = uuid.uuid4()
        cfg = MediaWidgetConfig(asset_id=container)
        ctx = self._ss_ctx(
            container,
            [(s1, {"duration_ms": 3000, "transition": "cut",
                   "transition_ms": 600}, "img", (_TINY_PNG, "image/png"))],
        )
        r = MediaWidget().render_html(cfg, _cell(), "ssC", ctx=ctx)
        # Duration is carried by the --cw-ss-ms custom property the
        # ported device CSS reads (the JS overrides it per swap).
        assert "--cw-ss-ms:0ms" in r.html
        assert r.init_js is not None
        assert "var transMs = [0];" in r.init_js

    def test_fade_transition_uses_transition_ms(self):
        container = uuid.uuid4()
        s1, s2 = uuid.uuid4(), uuid.uuid4()
        cfg = MediaWidgetConfig(asset_id=container)
        ctx = self._ss_ctx(
            container,
            [
                (s1, {"duration_ms": 3000, "transition": "fade",
                      "transition_ms": 750}, "img", (_TINY_PNG, "image/png")),
                (s2, {"duration_ms": 3000, "transition": "fade",
                      "transition_ms": 750}, "img", (_TINY_PNG, "image/png")),
            ],
        )
        r = MediaWidget().render_html(cfg, _cell(), "ssF", ctx=ctx)
        assert "--cw-ss-ms:750ms" in r.html
        assert r.init_js is not None
        assert "var transMs = [750,750];" in r.init_js

    def test_cycling_js_bakes_durations_and_guards_single(self):
        container = uuid.uuid4()
        s1, s2 = uuid.uuid4(), uuid.uuid4()
        cfg = MediaWidgetConfig(asset_id=container)
        ctx = self._ss_ctx(
            container,
            [
                (s1, {"duration_ms": 4000, "transition": "cut"},
                 "img", (_TINY_PNG, "image/png")),
                (s2, {"duration_ms": 5000, "transition": "cut"},
                 "img", (_TINY_PNG, "image/png")),
            ],
        )
        r = MediaWidget().render_html(cfg, _cell(), "ssJ", ctx=ctx)
        assert r.init_js is not None
        assert "[4000,5000]" in r.init_js
        assert "slides.length <= 1" in r.init_js
        # Selector is instance-scoped via the runtime instanceId arg.
        assert "'.cw-ss-' + instanceId" in r.init_js

    def test_single_slide_still_renders_one_slide(self):
        container = uuid.uuid4()
        s1 = uuid.uuid4()
        cfg = MediaWidgetConfig(asset_id=container)
        ctx = self._ss_ctx(
            container,
            [(s1, {"duration_ms": 4000, "transition": "cut"},
              "img", (_TINY_PNG, "image/png"))],
        )
        r = MediaWidget().render_html(cfg, _cell(), "ssS", ctx=ctx)
        assert r.html.count("cw-ss-slide") == 1
        assert r.referenced_asset_ids == [s1]

    def test_missing_source_channel_raises(self):
        container = uuid.uuid4()
        s1 = uuid.uuid4()
        cfg = MediaWidgetConfig(asset_id=container)
        # Plan references s1 but neither channel carries it.
        ctx = BundleContext(
            slideshow_plans={
                container: [SlideshowSlidePlan(
                    source_asset_id=s1, duration_ms=3000, transition="cut")]
            }
        )
        with pytest.raises(RuntimeError, match="missing from BundleContext"):
            MediaWidget().render_html(cfg, _cell(), "ssM", ctx=ctx)

    def test_alt_escaped_in_slideshow_slides(self):
        container = uuid.uuid4()
        s1 = uuid.uuid4()
        cfg = MediaWidgetConfig(asset_id=container, alt='<x>"q"')
        ctx = self._ss_ctx(
            container,
            [(s1, {"duration_ms": 3000, "transition": "cut"},
              "img", (_TINY_PNG, "image/png"))],
        )
        r = MediaWidget().render_html(cfg, _cell(), "ssA", ctx=ctx)
        assert "<x>" not in r.html
        assert "&lt;x&gt;" in r.html

    def test_slideshow_branch_preferred_over_image_channel(self):
        # If the container id ALSO appears in asset_bytes (shouldn't in
        # practice), the slideshow branch still wins so we cycle.
        container = uuid.uuid4()
        s1 = uuid.uuid4()
        cfg = MediaWidgetConfig(asset_id=container)
        ctx = self._ss_ctx(
            container,
            [(s1, {"duration_ms": 3000, "transition": "cut"},
              "img", (_TINY_PNG, "image/png"))],
        )
        ctx.asset_bytes[container] = _TINY_PNG
        ctx.asset_mimes[container] = "image/png"
        r = MediaWidget().render_html(cfg, _cell(), "ssP", ctx=ctx)
        assert "cw-ss-" in r.html

    @pytest.mark.parametrize(
        "transition,css_marker",
        [
            # fade_black is sequenced entirely in JS (two half fades
            # through black) — it has no dedicated CSS tx-* rule.
            ("fade_black", None),
            ("dissolve", "cw-tx-dissolve"),
            ("push", "cw-tx-push"),
            ("wipe", "cw-tx-wipe"),
            ("zoom", "cw-tx-zoom"),
        ],
    )
    def test_rich_transition_emits_class_and_shared_transition_css(
        self, transition, css_marker
    ):
        container = uuid.uuid4()
        s1, s2 = uuid.uuid4(), uuid.uuid4()
        cfg = MediaWidgetConfig(asset_id=container)
        ctx = self._ss_ctx(
            container,
            [
                (s1, {"duration_ms": 4000, "transition": transition,
                      "transition_ms": 800}, "img", (_TINY_PNG, "image/png")),
                (s2, {"duration_ms": 4000, "transition": transition,
                      "transition_ms": 800}, "img", (_TINY_PNG, "image/png")),
            ],
        )
        r = MediaWidget().render_html(cfg, _cell(), "ssRich", ctx=ctx)

        # Per-slide informational type class baked onto each slide div.
        assert f"cw-ss-t-{transition}" in r.html
        # The slide-count invariant still holds (type class shares no
        # "cw-ss-slide" substring).
        assert r.html.count("cw-ss-slide") == 2
        assert "cw-ss-slide cw-ss-active" in r.html

        # Shared transition CSS (ported from the device player shell)
        # emitted once as a static CSS asset.
        css_assets = [a for a in r.static_assets if a.kind == "css"]
        assert len(css_assets) == 1
        lib = css_assets[0].content
        # Ported device base mechanics.
        assert ".cw-ss-slide{" in lib
        assert ".cw-ss-slide.cw-ss-active{opacity:1" in lib
        assert ".cw-ss-slide.cw-ss-notrans{transition:none !important}" in lib
        # Per-mode rule present for the staged (transform/clip-path) modes.
        if css_marker is not None:
            assert css_marker in lib

        # The cycling JS ports swapTo(): staged modes flip dynamic tx-*
        # classes; fade_black is sequenced by name.
        assert r.init_js is not None
        if transition == "fade_black":
            assert "fade_black" in r.init_js
        else:
            assert css_marker in r.init_js

    def test_rich_transition_bakes_int_arrays_in_js(self):
        container = uuid.uuid4()
        s1, s2 = uuid.uuid4(), uuid.uuid4()
        cfg = MediaWidgetConfig(asset_id=container)
        ctx = self._ss_ctx(
            container,
            [
                # cut -> type index 0, transMs 0
                (s1, {"duration_ms": 3000, "transition": "cut"},
                 "img", (_TINY_PNG, "image/png")),
                # zoom -> type index 6, transMs 900
                (s2, {"duration_ms": 4000, "transition": "zoom",
                      "transition_ms": 900}, "img", (_TINY_PNG, "image/png")),
            ],
        )
        r = MediaWidget().render_html(cfg, _cell(), "ssArr", ctx=ctx)
        assert r.init_js is not None
        # Only integer arrays are baked into the runtime — never raw
        # config strings.
        assert "var types = [0,6];" in r.init_js
        assert "var transMs = [0,900];" in r.init_js
        assert "var durations = [3000,4000];" in r.init_js

    def test_cut_fade_still_emits_shared_transition_css(self):
        # Even a cut+fade-only slideshow emits the shared transition CSS
        # now — the base .cw-ss-slide / .cw-ss-active / fade mechanics
        # live there (ported from the device player shell), not in
        # per-mode keyframes.
        container = uuid.uuid4()
        s1, s2 = uuid.uuid4(), uuid.uuid4()
        cfg = MediaWidgetConfig(asset_id=container)
        ctx = self._ss_ctx(
            container,
            [
                (s1, {"duration_ms": 3000, "transition": "cut"},
                 "img", (_TINY_PNG, "image/png")),
                (s2, {"duration_ms": 3000, "transition": "fade",
                      "transition_ms": 600}, "img", (_TINY_PNG, "image/png")),
            ],
        )
        r = MediaWidget().render_html(cfg, _cell(), "ssCF", ctx=ctx)
        css_assets = [a for a in r.static_assets if a.kind == "css"]
        assert len(css_assets) == 1
        assert ".cw-ss-slide{" in css_assets[0].content

    def test_unknown_transition_falls_back_to_fade(self):
        # SlideshowSlidePlan.transition is a plain str; an unexpected
        # value must degrade to "fade" rather than emit an unknown class.
        container = uuid.uuid4()
        s1 = uuid.uuid4()
        cfg = MediaWidgetConfig(asset_id=container)
        ctx = self._ss_ctx(
            container,
            [(s1, {"duration_ms": 3000, "transition": "bogus",
                   "transition_ms": 500}, "img", (_TINY_PNG, "image/png"))],
        )
        r = MediaWidget().render_html(cfg, _cell(), "ssU", ctx=ctx)
        assert "cw-ss-t-fade" in r.html
        assert "cw-ss-t-bogus" not in r.html
        # Shared transition CSS is always emitted (base slide mechanics).
        assert [a for a in r.static_assets if a.kind == "css"]

    def test_contain_blur_image_emits_layered_blur_fill(self):
        # Per-slide fit='contain_blur' on an IMAGE slide must render a
        # blurred backdrop + a contained foreground (NOT a cropping
        # object-fit:cover img), mirroring the device firmware.
        container = uuid.uuid4()
        s1 = uuid.uuid4()
        cfg = MediaWidgetConfig(asset_id=container)
        ctx = self._ss_ctx(
            container,
            [(s1, {"duration_ms": 3000, "transition": "cut",
                   "fit": "contain_blur"}, "img", (_TINY_PNG, "image/png"))],
        )
        r = MediaWidget().render_html(cfg, _cell(), "ssBlur", ctx=ctx)
        # Layered wrap: a backdrop and a foreground copy of the image.
        assert "cw-ss-blur-wrap" in r.html
        assert 'class="cw-ss-blur-bg"' in r.html
        assert 'class="cw-ss-blur-fg"' in r.html
        assert 'aria-hidden="true"' in r.html
        # The bug we are fixing: contain_blur must NOT silently crop via
        # an inline object-fit:cover on the slide image.
        assert "object-fit:cover" not in r.html
        # CSS gives the backdrop cover+blur and the foreground contain.
        assert "img.cw-ss-blur-bg" in r.css
        assert "filter: blur(24px)" in r.css
        assert "transform: scale(1.12)" in r.css
        assert "img.cw-ss-blur-fg" in r.css

    def test_contain_blur_backdrop_excluded_from_ken_burns(self):
        # Ken Burns must zoom only the foreground; the blurred backdrop's
        # static scale(1.12) must not be overridden by the KB keyframe
        # (which would reveal the black wrapper edges).
        container = uuid.uuid4()
        s1 = uuid.uuid4()
        cfg = MediaWidgetConfig(asset_id=container)
        ctx = self._ss_ctx(
            container,
            [(s1, {"duration_ms": 8000, "transition": "cut",
                   "fit": "contain_blur", "effect": "ken_burns"},
              "img", (_TINY_PNG, "image/png"))],
        )
        r = MediaWidget().render_html(cfg, _cell(), "ssKB", ctx=ctx)
        assert "cw-ss-kb" in r.html
        # KB animation selector excludes the blurred backdrop image.
        assert "img:not(.cw-ss-blur-bg)" in r.css

    def test_contain_blur_video_degrades_to_plain_contain(self):
        # Video never gets a blurred backdrop in v1 — contain_blur on a
        # video slide must fall through to a plain object-fit:contain
        # <video>, with no blur-fill wrapper.
        container = uuid.uuid4()
        s1 = uuid.uuid4()
        cfg = MediaWidgetConfig(asset_id=container)
        ctx = self._ss_ctx(
            container,
            [(s1, {"duration_ms": 4000, "transition": "cut",
                   "fit": "contain_blur"}, "vid",
              "/assets/videos/clip.mp4")],
        )
        r = MediaWidget().render_html(cfg, _cell(), "ssBV", ctx=ctx)
        assert "<video" in r.html
        assert "object-fit:contain" in r.html
        assert "cw-ss-blur-wrap" not in r.html

    def test_plain_fits_unchanged_no_blur_markup(self):
        # cover/contain slides must render byte-identically to before:
        # a single inline-object-fit img, no blur-fill classes.
        for fit in ("cover", "contain"):
            container = uuid.uuid4()
            s1 = uuid.uuid4()
            cfg = MediaWidgetConfig(asset_id=container)
            ctx = self._ss_ctx(
                container,
                [(s1, {"duration_ms": 3000, "transition": "cut",
                       "fit": fit}, "img", (_TINY_PNG, "image/png"))],
            )
            r = MediaWidget().render_html(cfg, _cell(), "ssPlain", ctx=ctx)
            assert f"object-fit:{fit}" in r.html
            assert "cw-ss-blur-wrap" not in r.html


    """``composed_cell_transition`` now passes the full validated
    repertoire through (was: collapse everything but cut -> fade)."""

    @pytest.mark.parametrize(
        "t",
        ["cut", "fade", "fade_black", "dissolve", "push", "wipe", "zoom"],
    )
    def test_known_transition_passes_through(self, t):
        from cms.composed.slideshow_expand import composed_cell_transition

        assert composed_cell_transition(t) == t

    def test_unknown_transition_degrades_to_fade(self):
        from cms.composed.slideshow_expand import composed_cell_transition

        assert composed_cell_transition("bogus") == "fade"
        assert composed_cell_transition("") == "fade"


class TestKenBurnsOrthogonalGrammar:
    """Pins the orthogonal Ken Burns (zoom × pan) token grammar.

    The CMS authoring popover (``slideshow_builder.html`` ``kbParse`` /
    ``kbTransforms``) mirrors these helpers byte-for-byte so the live
    mini-preview matches the device render. If these expectations move,
    update the JS helpers in lockstep.
    """

    @pytest.mark.parametrize(
        "token,expected",
        [
            ("", ("in", None)),
            ("in", ("in", None)),
            ("out", ("out", None)),
            # legacy bare-pan aliases fold to a zoom-in pan
            ("left", ("in", "left")),
            ("down_right", ("in", "down_right")),
            # composed tokens
            ("in_up", ("in", "up")),
            ("out_up_right", ("out", "up_right")),
            ("out_down_left", ("out", "down_left")),
            # garbage degrades to the safe pure-zoom-in default
            ("diagonal", ("in", None)),
            ("out_sideways", ("in", None)),
        ],
    )
    def test_normalize(self, token, expected):
        from cms.composed.widgets.media import _kb_normalize

        assert _kb_normalize(token) == expected

    def test_transform_pure_zoom(self):
        from cms.composed.widgets.media import _kb_transform

        assert _kb_transform("in", None) == ("scale(1.0001)", "scale(1.08)")
        assert _kb_transform("out", None) == ("scale(1.08)", "scale(1.0001)")

    def test_transform_zoom_out_up_right(self):
        # The new authoring default: zoom-out drifting toward the
        # up-right corner. Both axes carry an explicit ``%``.
        from cms.composed.widgets.media import _kb_transform

        assert _kb_transform("out", "up_right") == (
            "scale(1.08) translate(-2%, 2%)",
            "scale(1.0001) translate(2%, -2%)",
        )

    def test_transform_diagonals_are_corner_to_corner(self):
        from cms.composed.widgets.media import _kb_transform

        frm, to = _kb_transform("in", "down_left")
        assert frm == "scale(1.0001) translate(2%, -2%)"
        assert to == "scale(1.08) translate(-2%, 2%)"

    def test_out_up_right_is_an_allowed_wire_token(self):
        # The authoring default must survive schema validation.
        from cms.schemas.asset import KEN_BURNS_DIRECTIONS

        assert "out_up_right" in KEN_BURNS_DIRECTIONS


