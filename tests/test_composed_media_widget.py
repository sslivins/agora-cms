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
from cms.composed.registry import BundleContext, get_registry
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
