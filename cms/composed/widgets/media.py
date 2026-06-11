"""Media widget — embeds a single Asset (IMAGE or VIDEO) into the bundle.

A unified palette tile that handles both image and video assets so
the editor doesn't have to expose two near-identical widgets.  The
publish layer (see :mod:`cms.composed.publish`) buckets each
declared asset ID into one of two parallel channels on the
:class:`BundleContext`:

* :attr:`BundleContext.asset_bytes` — for IMAGE assets, loaded from
  disk and inlined as a base64 ``data:`` URI inside an ``<img>``
  tag.  Same shape as the older :class:`~cms.composed.widgets.image.ImageWidget`.
* :attr:`BundleContext.sibling_asset_urls` — for VIDEO assets, the
  publish layer registers the video as a *sibling* asset on the
  device cache and stores the device-local URL here.  The rendered
  bundle's ``<video src>`` points at that URL; the device cache
  layer downloads the file separately.

Inlining a multi-megabyte MP4 as a base64 data URI is a non-starter
(payload bloat, decode pressure on the Pi); the sibling-URL channel
keeps the bundle small and lets the device cache do its job.

Sizing model mirrors :class:`~cms.composed.widgets.image.ImageWidget`:
the widget always fills its grid cell, and ``object_fit`` controls
how the source is scaled inside the cell.

Phase 1C scope: the firmware sibling-fetch protocol that wires
declared-but-not-yet-cached VIDEO assets into the device's
``/assets/videos/`` directory is deferred to Phase 1D.  Until then,
videos used in composed slides must be independently assigned to the
same device groups so they land in the device's video cache through
the existing per-asset sync path.
"""

from __future__ import annotations

import base64
import html as _html
import logging
import uuid
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from cms.composed.registry import (
    BundleContext,
    Widget,
    WidgetRender,
    WidgetStaticAsset,
)
from cms.composed.schema import Cell


log = logging.getLogger(__name__)


# Soft warning threshold for inlined image size.  Matches the
# ImageWidget threshold — we hit this on a re-uploaded 4K photo that
# someone dropped into a 200px cell.
_LARGE_IMAGE_WARN_BYTES: int = 2 * 1024 * 1024


# The slideshow transition repertoire the composed media cell renders,
# in a fixed order.  The renderer bakes ONLY the integer index of each
# slide's transition into the runtime JS (never the raw string), and
# the JS maps the index back through an identical hard-coded array.
# The class-name suffix (used in CSS) is the validated transition token
# itself.  Keep this list in sync with the JS ``CW_SS_TYPES`` array in
# ``_render_slideshow``.
_SS_TRANSITIONS: tuple[str, ...] = (
    "cut",
    "fade",
    "fade_black",
    "dissolve",
    "push",
    "wipe",
    "zoom",
)
# Transitions driven by CSS @keyframes (enter/leave) rather than the
# plain opacity transition (fade) or an instant swap (cut).
_SS_KEYFRAME_TRANSITIONS: frozenset[str] = frozenset(
    {"fade_black", "dissolve", "push", "wipe", "zoom"}
)


# Shared keyframe library for the keyframe-driven slideshow transitions.
# Emitted once per bundle (de-duped by content hash).  Every keyframe
# sets ``opacity`` explicitly so fill-mode never leaves a slide in an
# ambiguous resting state.  The ``-in`` keyframe animates the incoming
# slide; the ``-out`` keyframe animates the outgoing one.  Per-instance
# ``animation-duration`` is set in JS so both halves stay in sync.
_SS_KEYFRAME_CSS: str = (
    "@keyframes cw-ss-fadeblack-in{0%{opacity:0}50%{opacity:0}100%{opacity:1}}\n"
    "@keyframes cw-ss-fadeblack-out{0%{opacity:1}50%{opacity:0}100%{opacity:0}}\n"
    "@keyframes cw-ss-dissolve-in{0%{opacity:0;transform:scale(1.06)}"
    "100%{opacity:1;transform:scale(1)}}\n"
    "@keyframes cw-ss-dissolve-out{0%{opacity:1}100%{opacity:0}}\n"
    "@keyframes cw-ss-push-in{0%{opacity:1;transform:translateX(100%)}"
    "100%{opacity:1;transform:translateX(0)}}\n"
    "@keyframes cw-ss-push-out{0%{opacity:1;transform:translateX(0)}"
    "100%{opacity:1;transform:translateX(-100%)}}\n"
    "@keyframes cw-ss-wipe-in{0%{opacity:1;clip-path:inset(0 0 0 100%)}"
    "100%{opacity:1;clip-path:inset(0 0 0 0)}}\n"
    "@keyframes cw-ss-wipe-out{0%{opacity:1}100%{opacity:1}}\n"
    "@keyframes cw-ss-zoom-in{0%{opacity:0;transform:scale(0.6)}"
    "100%{opacity:1;transform:scale(1)}}\n"
    "@keyframes cw-ss-zoom-out{0%{opacity:1}100%{opacity:0}}\n"
    ".cw-ss-enter-fade_black,.cw-ss-leave-fade_black,"
    ".cw-ss-enter-dissolve,.cw-ss-leave-dissolve,"
    ".cw-ss-enter-push,.cw-ss-leave-push,"
    ".cw-ss-enter-wipe,.cw-ss-leave-wipe,"
    ".cw-ss-enter-zoom,.cw-ss-leave-zoom{"
    "animation-timing-function:ease-in-out;animation-fill-mode:both}\n"
    ".cw-ss-enter-fade_black{animation-name:cw-ss-fadeblack-in}\n"
    ".cw-ss-leave-fade_black{animation-name:cw-ss-fadeblack-out}\n"
    ".cw-ss-enter-dissolve{animation-name:cw-ss-dissolve-in}\n"
    ".cw-ss-leave-dissolve{animation-name:cw-ss-dissolve-out}\n"
    ".cw-ss-enter-push{animation-name:cw-ss-push-in}\n"
    ".cw-ss-leave-push{animation-name:cw-ss-push-out}\n"
    ".cw-ss-enter-wipe{animation-name:cw-ss-wipe-in}\n"
    ".cw-ss-leave-wipe{animation-name:cw-ss-wipe-out}\n"
    ".cw-ss-enter-zoom{animation-name:cw-ss-zoom-in}\n"
    ".cw-ss-leave-zoom{animation-name:cw-ss-zoom-out}\n"
)


class MediaWidgetConfig(BaseModel):
    """User-editable config for :class:`MediaWidget`.

    A single ``asset_id`` points at either an IMAGE or a VIDEO asset;
    which one is determined at publish time (the publish layer looks
    up the Asset row and routes by ``asset_type``).
    """

    model_config = ConfigDict(extra="forbid")

    asset_id: uuid.UUID
    # Mirrors the CSS ``object-fit`` allowlist we actually support.
    # Applied to both ``<img>`` and ``<video>``; both honour it.
    object_fit: Literal["cover", "contain", "fill"] = "cover"
    # Optional alt text for images / accessibility hint for videos.
    # Always HTML-escaped before emission.
    alt: str = Field(default="", max_length=512)


class MediaWidget(Widget):
    """Single image OR video asset; publish layer picks the channel."""

    slug: ClassVar[str] = "media"
    display_name: ClassVar[str] = "Media"
    icon: ClassVar[str] = "🎬"
    ConfigSchema: ClassVar[type[BaseModel]] = MediaWidgetConfig
    config_version: ClassVar[int] = 1

    def default_config(self) -> dict:
        # All-zero UUID placeholder is intentional — the editor will
        # replace it as soon as the user picks an asset.  validate_layout
        # rejects it because the referenced asset doesn't exist, forcing
        # an explicit pick before save.
        return {
            "asset_id": "00000000-0000-0000-0000-000000000000",
            "object_fit": "cover",
            "alt": "",
        }

    def editor_template(self) -> str:
        return "composed/widgets/media.html"

    def declared_asset_ids(self, config: BaseModel) -> list[uuid.UUID]:
        assert isinstance(config, MediaWidgetConfig)
        return [config.asset_id]

    def render_html(
        self,
        config: BaseModel,
        cell: Cell,
        instance_id: str,
        ctx: BundleContext | None = None,
    ) -> WidgetRender:
        assert isinstance(config, MediaWidgetConfig), (
            "MediaWidget.render_html expects a MediaWidgetConfig instance"
        )
        if ctx is None:
            raise RuntimeError(
                "MediaWidget.render_html requires a BundleContext "
                "with the declared asset routed into either "
                "asset_bytes (image) or sibling_asset_urls (video)"
            )

        asset_id = config.asset_id
        css_class = f"cw-media-{instance_id}"
        alt_escaped = _html.escape(config.alt)

        # Slideshow branch: the declared asset is a SLIDESHOW container.
        # The publish / render layer resolved its ordered slides into
        # ctx.slideshow_plans and routed each per-slide source into
        # asset_bytes (image) or sibling_asset_urls (video).  Emit a
        # stack of absolutely-positioned slides and a small timer that
        # cross-fades / cuts between them client-side.
        if ctx.slideshow_plans and asset_id in ctx.slideshow_plans:
            return self._render_slideshow(
                config=config,
                instance_id=instance_id,
                asset_id=asset_id,
                alt_escaped=alt_escaped,
                ctx=ctx,
            )

        # Video branch: publish layer routed this ID to the sibling
        # URL channel.  Emit a <video> tag pointing at the device-local
        # URL; the bundle stays small and the device cache fetches the
        # MP4 through the existing per-asset sync path.
        if asset_id in ctx.sibling_asset_urls:
            src = ctx.sibling_asset_urls[asset_id]
            # The publish layer is responsible for URL-encoding the
            # filename.  Escape for HTML attribute safety here.
            src_escaped = _html.escape(src, quote=True)
            html_out = (
                f'<video class="{css_class}" '
                f'src="{src_escaped}" '
                f'muted loop autoplay playsinline '
                f'aria-label="{alt_escaped}"></video>'
            )
        elif asset_id in ctx.asset_bytes:
            # Image branch: inline as base64 data URI.  Mirrors the
            # ImageWidget shape exactly so existing image rendering
            # behaviour is unchanged.
            blob = ctx.asset_bytes[asset_id]
            mime = ctx.asset_mimes[asset_id]

            if len(blob) > _LARGE_IMAGE_WARN_BYTES:
                log.warning(
                    "composed media widget %s: image asset %s is %d bytes "
                    "(>%d) — inlined bundle will be large",
                    instance_id,
                    asset_id,
                    len(blob),
                    _LARGE_IMAGE_WARN_BYTES,
                )

            b64 = base64.b64encode(blob).decode("ascii")
            data_uri = f"data:{mime};base64,{b64}"
            html_out = (
                f'<img class="{css_class}" '
                f'src="{data_uri}" '
                f'alt="{alt_escaped}" '
                f'draggable="false" />'
            )
        else:
            # Bundle builder enforces declare-before-reference and the
            # publish layer always routes a declared ID into one of the
            # two channels — getting here means a regression in that
            # contract.  Fail loudly rather than emit a broken tag.
            raise RuntimeError(
                f"MediaWidget instance {instance_id}: asset {asset_id} "
                "missing from BundleContext (neither asset_bytes nor "
                "sibling_asset_urls)"
            )

        css_out = (
            f".{css_class} {{\n"
            f"  width: 100%;\n"
            f"  height: 100%;\n"
            f"  display: block;\n"
            f"  object-fit: {config.object_fit};\n"
            f"  object-position: center;\n"
            f"  user-select: none;\n"
            f"  background: #000;\n"
            f"}}"
        )

        return WidgetRender(
            html=html_out,
            css=css_out,
            referenced_asset_ids=[asset_id],
        )

    def _render_slideshow(
        self,
        config: MediaWidgetConfig,
        instance_id: str,
        asset_id: uuid.UUID,
        alt_escaped: str,
        ctx: BundleContext,
    ) -> WidgetRender:
        """Render a SLIDESHOW asset as a self-cycling stack of slides.

        Each member slide is rendered exactly like a standalone media
        widget (image inlined as a data URI, video pointed at its
        device-local sibling URL), stacked absolutely inside an
        instance-scoped container.  Slide 0 is marked active in the
        markup so a t=0 thumbnail snapshot is deterministic.  A small
        baked-in timer advances the stack, honouring each slide's
        entrance transition: ``cut`` is an instant swap and ``fade`` a
        plain opacity cross-fade, while ``fade_black`` / ``dissolve`` /
        ``push`` / ``wipe`` / ``zoom`` are driven by self-contained CSS
        ``@keyframes`` (enter/leave animation classes applied in JS).
        These are self-contained approximations of the device's native
        firmware transitions, not pixel-exact reproductions.
        """
        plans = ctx.slideshow_plans[asset_id]
        if not plans:
            # Should never happen — publish/render reject empty
            # slideshows up front — but fail loud rather than emit an
            # empty, never-advancing container.
            raise RuntimeError(
                f"MediaWidget instance {instance_id}: slideshow asset "
                f"{asset_id} expanded to zero slides"
            )

        css_class = f"cw-ss-{instance_id}"
        slide_html: list[str] = []
        referenced: list[uuid.UUID] = []
        durations: list[int] = []
        trans_codes: list[int] = []
        trans_durations: list[int] = []

        for idx, plan in enumerate(plans):
            source = plan.source_asset_id
            referenced.append(source)
            durations.append(int(plan.duration_ms))

            active = " cw-ss-active" if idx == 0 else ""
            # Per-slide entrance transition. "cut" is an instant swap
            # (0ms); every other transition animates over transition_ms.
            # The transition type is validated against SLIDE_TRANSITIONS
            # upstream; fall back to "fade" for anything unexpected so
            # the cell still cycles rather than emitting an unknown class.
            transition = plan.transition if plan.transition in _SS_TRANSITIONS else "fade"
            trans_ms = 0 if transition == "cut" else int(plan.transition_ms)
            trans_codes.append(_SS_TRANSITIONS.index(transition))
            trans_durations.append(trans_ms)
            slide_style = f"transition-duration:{trans_ms}ms"

            if source in ctx.sibling_asset_urls:
                src = ctx.sibling_asset_urls[source]
                src_escaped = _html.escape(src, quote=True)
                inner = (
                    f'<video src="{src_escaped}" '
                    f"muted loop playsinline "
                    f'aria-label="{alt_escaped}"></video>'
                )
            elif source in ctx.asset_bytes:
                blob = ctx.asset_bytes[source]
                mime = ctx.asset_mimes[source]
                if len(blob) > _LARGE_IMAGE_WARN_BYTES:
                    log.warning(
                        "composed media widget %s: slideshow image asset "
                        "%s is %d bytes (>%d) — inlined bundle will be large",
                        instance_id,
                        source,
                        len(blob),
                        _LARGE_IMAGE_WARN_BYTES,
                    )
                b64 = base64.b64encode(blob).decode("ascii")
                data_uri = f"data:{mime};base64,{b64}"
                inner = (
                    f'<img src="{data_uri}" '
                    f'alt="{alt_escaped}" draggable="false" />'
                )
            else:
                # Same contract violation as the standalone else branch:
                # the build layer must route every per-slide source into
                # one of the two channels.
                raise RuntimeError(
                    f"MediaWidget instance {instance_id}: slideshow source "
                    f"{source} missing from BundleContext (neither "
                    "asset_bytes nor sibling_asset_urls)"
                )

            slide_classes = f"cw-ss-slide{active} cw-ss-t-{transition}"
            slide_html.append(
                f'<div class="{slide_classes}" style="{slide_style}">'
                f"{inner}</div>"
            )

        html_out = f'<div class="{css_class}">' + "".join(slide_html) + "</div>"

        uses_keyframes = any(
            _SS_TRANSITIONS[c] in _SS_KEYFRAME_TRANSITIONS for c in trans_codes
        )

        css_out = (
            f".{css_class} {{\n"
            f"  position: relative;\n"
            f"  width: 100%;\n"
            f"  height: 100%;\n"
            f"  overflow: hidden;\n"
            f"  background: #000;\n"
            f"}}\n"
            f".{css_class} .cw-ss-slide {{\n"
            f"  position: absolute;\n"
            f"  inset: 0;\n"
            f"  opacity: 0;\n"
            f"  z-index: 1;\n"
            f"  transition-property: opacity;\n"
            f"  transition-timing-function: ease-in-out;\n"
            f"}}\n"
            f".{css_class} .cw-ss-slide.cw-ss-active {{\n"
            f"  opacity: 1;\n"
            f"  z-index: 2;\n"
            f"}}\n"
            # Keyframe-driven transitions own their opacity timeline, so
            # the base opacity transition must not fight the animation.
            f".{css_class} .cw-ss-t-fade_black,\n"
            f".{css_class} .cw-ss-t-dissolve,\n"
            f".{css_class} .cw-ss-t-push,\n"
            f".{css_class} .cw-ss-t-wipe,\n"
            f".{css_class} .cw-ss-t-zoom {{\n"
            f"  transition: none;\n"
            f"}}\n"
            f".{css_class} img,\n"
            f".{css_class} video {{\n"
            f"  width: 100%;\n"
            f"  height: 100%;\n"
            f"  display: block;\n"
            f"  object-fit: {config.object_fit};\n"
            f"  object-position: center;\n"
            f"  user-select: none;\n"
            f"}}"
        )

        # Bake ONLY integer arrays into the runtime — never interpolate
        # raw config strings into JS.  ``types`` are indices into the JS
        # ``TYPE`` array (kept in sync with ``_SS_TRANSITIONS``);
        # ``transMs`` are the per-slide transition durations.
        durations_js = "[" + ",".join(str(d) for d in durations) + "]"
        types_js = "[" + ",".join(str(c) for c in trans_codes) + "]"
        trans_ms_js = "[" + ",".join(str(d) for d in trans_durations) + "]"
        init_js = (
            "var root = document.querySelector('.cw-ss-' + instanceId);\n"
            "if (!root) { return; }\n"
            "var slides = root.querySelectorAll('.cw-ss-slide');\n"
            f"var durations = {durations_js};\n"
            f"var types = {types_js};\n"
            f"var transMs = {trans_ms_js};\n"
            "var TYPE = ['cut','fade','fade_black','dissolve','push','wipe','zoom'];\n"
            "var ANIM = {fade_black:1,dissolve:1,push:1,wipe:1,zoom:1};\n"
            "function startVideo(slide) {\n"
            "  var v = slide.querySelector('video');\n"
            "  if (v) { try { v.currentTime = 0; var p = v.play();"
            " if (p && p.catch) { p.catch(function(){}); } } catch (e) {} }\n"
            "}\n"
            "if (slides.length >= 1) { startVideo(slides[0]); }\n"
            "if (slides.length <= 1) { return; }\n"
            "function clearAnim(s) {\n"
            "  var rm = [];\n"
            "  s.classList.forEach(function(c){\n"
            "    if (c.indexOf('cw-ss-enter-') === 0 || c.indexOf('cw-ss-leave-') === 0)"
            " { rm.push(c); }\n"
            "  });\n"
            "  for (var i = 0; i < rm.length; i++) { s.classList.remove(rm[i]); }\n"
            "  s.style.animationDuration = '';\n"
            "}\n"
            "var idx = 0;\n"
            "function activate(n, prev) {\n"
            "  for (var i = 0; i < slides.length; i++) { clearAnim(slides[i]); }\n"
            "  void root.offsetWidth;\n"
            "  for (var j = 0; j < slides.length; j++) {\n"
            "    slides[j].classList.toggle('cw-ss-active', j === n);\n"
            "  }\n"
            "  var t = TYPE[types[n]];\n"
            "  if (ANIM[t]) {\n"
            "    var dur = transMs[n] + 'ms';\n"
            "    var incoming = slides[n];\n"
            "    incoming.classList.add('cw-ss-enter-' + t);\n"
            "    incoming.style.animationDuration = dur;\n"
            "    if (prev != null && prev !== n) {\n"
            "      var outgoing = slides[prev];\n"
            "      outgoing.classList.add('cw-ss-leave-' + t);\n"
            "      outgoing.style.animationDuration = dur;\n"
            "    }\n"
            "  }\n"
            "  startVideo(slides[n]);\n"
            "}\n"
            "function schedule() {\n"
            "  setTimeout(function () {\n"
            "    var prev = idx;\n"
            "    idx = (idx + 1) % slides.length;\n"
            "    activate(idx, prev);\n"
            "    schedule();\n"
            "  }, durations[idx]);\n"
            "}\n"
            "schedule();"
        )

        static_assets: list[WidgetStaticAsset] = []
        if uses_keyframes:
            # Shared keyframe library — identical bytes for every
            # slideshow-media instance, so the bundle builder de-dupes
            # it to a single <style> block by content hash.  The
            # enter/leave animation-name rules are intentionally global
            # (not instance-scoped): JS only ever applies the classes to
            # slides inside this instance's root during a transition.
            static_assets.append(
                WidgetStaticAsset(
                    kind="css",
                    mime="text/css",
                    content=_SS_KEYFRAME_CSS,
                )
            )

        return WidgetRender(
            html=html_out,
            css=css_out,
            init_js=init_js,
            referenced_asset_ids=referenced,
            static_assets=static_assets,
        )
