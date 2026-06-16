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
# The composed media cell renders slideshow transitions by PORTING the
# device player shell's real transition mechanism — two crossfading
# layers plus per-mode ``tx-*`` classes flipped by JS — rather than
# approximating them with a parallel ``@keyframes`` library.  The CSS
# below is a near-verbatim port of ``agora/player/shell/player.css``'s
# ``.layer`` / ``.tx-*`` rules, and the cycling JS in
# ``_render_slideshow`` ports the ``swapTo()`` flip logic from
# ``player.js`` (the transitions-off → commit entry state → flush →
# flip ``.active`` dance, plus the ``fade_black`` two-stage sequenced
# fade).  An embedded slideshow therefore animates pixel-identically to
# a standalone one on the device.
#
# Device → composed class-name mapping:
#   .layer          -> .cw-ss-slide
#   .layer.active   -> .cw-ss-slide.cw-ss-active
#   .no-transition  -> .cw-ss-notrans
#   .tx-<mode>      -> .cw-tx-<mode>
#   .tx-incoming    -> .cw-tx-incoming
#   .tx-outgoing    -> .cw-tx-outgoing
# The device drives timing off a ``--transition-ms`` custom property set
# per-swap in JS; we use ``--cw-ss-ms`` for the same role.
#
# Emitted once per bundle (de-duped by content hash) as a shared static
# CSS asset.  The selectors are intentionally global (not instance-
# scoped) because the cycling JS only ever toggles the dynamic
# ``cw-tx-*`` / ``cw-ss-active`` classes on slides inside its own
# instance root, and the base ``.cw-ss-slide`` mechanics are identical
# for every slideshow-media cell.
#
# One intentional deviation from the device's fixed two-layer DOM: an
# N-slide stack needs explicit z-index so the transitioning incoming /
# outgoing slides composite above the resting (opacity:0) slides, with
# the incoming above the outgoing so wipe / push reveal correctly
# regardless of slide order (the device relies on a fixed 2-element DOM
# order for this).
_SS_TRANSITION_CSS: str = (
    ".cw-ss-slide{"
    "position:absolute;inset:0;opacity:0;z-index:1;"
    "transition:"
    "opacity var(--cw-ss-ms,600ms) ease-in-out,"
    "transform var(--cw-ss-ms,600ms) ease-in-out,"
    "clip-path var(--cw-ss-ms,600ms) ease-in-out;"
    "will-change:opacity,transform,clip-path}\n"
    ".cw-ss-slide.cw-ss-active{opacity:1;z-index:2}\n"
    ".cw-ss-slide.cw-ss-notrans{transition:none !important}\n"
    ".cw-ss-slide.cw-tx-outgoing{z-index:3}\n"
    ".cw-ss-slide.cw-tx-incoming{z-index:4}\n"
    # push: incoming slides in from the right, outgoing off to the left;
    # opacity pinned at 1 so the black gutter never shows.
    ".cw-ss-slide.cw-tx-push{opacity:1}\n"
    ".cw-ss-slide.cw-tx-push:not(.cw-ss-active){transform:translateX(100%)}\n"
    ".cw-ss-slide.cw-tx-push.cw-ss-active{transform:translateX(0)}\n"
    ".cw-ss-slide.cw-tx-push.cw-tx-outgoing:not(.cw-ss-active)"
    "{transform:translateX(-100%)}\n"
    # wipe: incoming reveals L->R via a clip-path inset; outgoing stays put.
    ".cw-ss-slide.cw-tx-wipe{opacity:1}\n"
    ".cw-ss-slide.cw-tx-wipe:not(.cw-ss-active){clip-path:inset(0 100% 0 0)}\n"
    ".cw-ss-slide.cw-tx-wipe.cw-ss-active{clip-path:inset(0 0 0 0)}\n"
    ".cw-ss-slide.cw-tx-wipe.cw-tx-outgoing{clip-path:inset(0 0 0 0)}\n"
    # dissolve (Ken Burns): crossfade + outgoing scales up to 1.05.
    ".cw-ss-slide.cw-tx-dissolve.cw-tx-outgoing:not(.cw-ss-active)"
    "{transform:scale(1.05)}\n"
    ".cw-ss-slide.cw-tx-dissolve.cw-tx-incoming.cw-ss-active"
    "{transform:scale(1)}\n"
    # zoom: incoming scales 0.9 -> 1.0 while fading in.
    ".cw-ss-slide.cw-tx-zoom:not(.cw-ss-active){transform:scale(0.9)}\n"
    ".cw-ss-slide.cw-tx-zoom.cw-ss-active{transform:scale(1)}\n"
    # fade_black is sequenced JS-side (two half-duration fades through the
    # black container background) — no extra CSS needed here.
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
        entrance transition.

        Transitions are a faithful PORT of the device player shell
        (``agora/player/shell/player.{css,js}``), not approximations:
        the shared ``_SS_TRANSITION_CSS`` asset reproduces the shell's
        ``.layer`` / ``tx-*`` rules and the baked JS ports ``swapTo()``
        (the transitions-off → commit entry state → flush → flip
        ``.active`` dance for ``push`` / ``wipe`` / ``dissolve`` /
        ``zoom``, and the two-stage ``fade_black`` sequence).  ``cut`` is
        an instant swap and ``fade`` a plain opacity cross-fade.  An
        embedded slideshow therefore animates pixel-identically to a
        standalone one on the device.
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
            # Per-slide display effect (manifest schema 1.3).  ``fit`` is
            # the CSS object-fit applied inline to this slide's media so it
            # overrides the widget-wide ``config.object_fit`` default;
            # ``ken_burns`` adds a slow zoom keyframe that runs while the
            # slide is active (see ``css_out`` below).
            #
            # ``contain_blur`` is NOT a plain object-fit value — it renders
            # the image contained over a blurred, zoomed copy of itself
            # (filling the letterbox bars).  Mirrors the device firmware
            # (agora/player/shell/player.js + player.css ``fit-blur-*``):
            # for images we emit a wrapper (backdrop + foreground); video
            # never gets a blurred backdrop in v1 and degrades to plain
            # ``contain``.
            blur_fill = plan.fit == "contain_blur"
            if plan.fit in ("cover", "contain"):
                fit = plan.fit
            elif blur_fill:
                # Foreground / video fallback object-fit.
                fit = "contain"
            else:
                fit = "cover"
            fit_style = f"object-fit:{fit}"
            kb = plan.effect == "ken_burns"
            # Per-slide resting default for the transition-duration custom
            # property the shared CSS reads.  The cycling JS overrides it
            # per-swap (and halves it for fade_black); this inline value is
            # just a sensible default for a static t=0 snapshot.  The
            # ``--cw-ss-kb-ms`` property spans the Ken Burns zoom across the
            # slide's display time.
            slide_style = (
                f"--cw-ss-ms:{trans_ms}ms;"
                f"--cw-ss-kb-ms:{int(plan.duration_ms)}ms"
            )

            if source in ctx.sibling_asset_urls:
                src = ctx.sibling_asset_urls[source]
                src_escaped = _html.escape(src, quote=True)
                inner = (
                    f'<video src="{src_escaped}" '
                    f'style="{fit_style}" '
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
                if blur_fill:
                    # Blur-fill: a blurred, zoomed backdrop copy fills the
                    # letterbox bars while the foreground stays contained.
                    # Backdrop/foreground object-fit comes from the
                    # ``cw-ss-blur-*`` classes (see ``css_out``); the
                    # backdrop is hidden from a11y and excluded from Ken
                    # Burns so the static scale never gets overridden.
                    inner = (
                        '<div class="cw-ss-blur-wrap">'
                        f'<img src="{data_uri}" class="cw-ss-blur-bg" '
                        f'aria-hidden="true" draggable="false" />'
                        f'<img src="{data_uri}" class="cw-ss-blur-fg" '
                        f'alt="{alt_escaped}" draggable="false" />'
                        "</div>"
                    )
                else:
                    inner = (
                        f'<img src="{data_uri}" '
                        f'style="{fit_style}" '
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

            kb_class = " cw-ss-kb" if kb else ""
            slide_classes = f"cw-ss-slide{active} cw-ss-t-{transition}{kb_class}"
            slide_html.append(
                f'<div class="{slide_classes}" style="{slide_style}">'
                f"{inner}</div>"
            )

        html_out = f'<div class="{css_class}">' + "".join(slide_html) + "</div>"

        # Instance-scoped CSS holds the container framing + media sizing.
        # The widget-wide ``object-fit`` rule below is the default; each
        # slideshow slide carries an inline ``object-fit`` (per-slide
        # ``fit``, schema 1.3) that overrides it.  The slide-transition
        # mechanics live in the shared, global ``_SS_TRANSITION_CSS``
        # asset emitted below.
        css_out = (
            f".{css_class} {{\n"
            f"  position: relative;\n"
            f"  width: 100%;\n"
            f"  height: 100%;\n"
            f"  overflow: hidden;\n"
            f"  background: #000;\n"
            f"}}\n"
            f".{css_class} img,\n"
            f".{css_class} video {{\n"
            f"  width: 100%;\n"
            f"  height: 100%;\n"
            f"  display: block;\n"
            f"  object-fit: {config.object_fit};\n"
            f"  object-position: center;\n"
            f"  user-select: none;\n"
            f"}}\n"
            # Blur-fill (per-slide fit='contain_blur', image slides only).
            # The foreground image is contained; a second copy is blown up
            # and blurred behind it to fill the letterbox bars, mirroring
            # the device firmware's ``fit-blur-*`` rules.  Two-class
            # selectors outrank the base ``object-fit`` above.  scale(1.12)
            # hides the blur halo's transparent edge.
            f".{css_class} .cw-ss-blur-wrap {{\n"
            f"  position: absolute;\n"
            f"  inset: 0;\n"
            f"  width: 100%;\n"
            f"  height: 100%;\n"
            f"  overflow: hidden;\n"
            f"}}\n"
            f".{css_class} img.cw-ss-blur-bg,\n"
            f".{css_class} img.cw-ss-blur-fg {{\n"
            f"  position: absolute;\n"
            f"  top: 0;\n"
            f"  left: 0;\n"
            f"}}\n"
            f".{css_class} img.cw-ss-blur-bg {{\n"
            f"  object-fit: cover;\n"
            f"  transform: scale(1.12);\n"
            f"  filter: blur(24px);\n"
            f"}}\n"
            f".{css_class} img.cw-ss-blur-fg {{\n"
            f"  object-fit: contain;\n"
            f"}}\n"
            # Ken Burns (manifest schema 1.3, per-slide effect='ken_burns').
            # A slow zoom that spans the slide's display time, applied only
            # while the slide is active so it restarts each time the slide
            # comes back around.  The keyframe is instance-scoped so two
            # cells on the same canvas don't share animation state.  Slides
            # without the effect carry no ``cw-ss-kb`` class and render
            # byte-identically to the pre-1.3 output.  The KB transform is
            # on the media element while transitions animate the slide
            # ``<div>``, so the two never fight over the same property.
            f".{css_class} .cw-ss-slide.cw-ss-kb.cw-ss-active "
            f"img:not(.cw-ss-blur-bg),\n"
            f".{css_class} .cw-ss-slide.cw-ss-kb.cw-ss-active video {{\n"
            f"  animation: cw-kb-{instance_id} var(--cw-ss-kb-ms, 10000ms)"
            f" ease-out forwards;\n"
            f"  transform-origin: center center;\n"
            f"}}\n"
            f"@keyframes cw-kb-{instance_id} {{\n"
            f"  from {{ transform: scale(1.0001); }}\n"
            f"  to {{ transform: scale(1.08); }}\n"
            f"}}"
        )

        # Bake ONLY integer arrays into the runtime — never interpolate
        # raw config strings into JS.  ``types`` are indices into the JS
        # ``TYPE`` array (kept in sync with ``_SS_TRANSITIONS``);
        # ``transMs`` are the per-slide transition durations.
        durations_js = "[" + ",".join(str(d) for d in durations) + "]"
        types_js = "[" + ",".join(str(c) for c in trans_codes) + "]"
        trans_ms_js = "[" + ",".join(str(d) for d in trans_durations) + "]"
        # Ported from agora/player/shell/player.js ``swapTo()``.  Two
        # crossfading layers on the device become an N-slide stack here,
        # but the per-mode class choreography is identical: set the
        # duration custom property, strip stale tx-* classes, then either
        # cut (instant), fade (plain opacity), fade_black (two sequenced
        # half fades through the black container), or run the single-stage
        # commit-with-transitions-off → flush → flip-active dance for
        # push/wipe/dissolve/zoom.  The device's _freezeOutgoingVideo
        # DRM-overlay workaround is deliberately omitted — the composed
        # cell composites in ordinary Chromium where it isn't needed.
        init_js = (
            "var root = document.querySelector('.cw-ss-' + instanceId);\n"
            "if (!root) { return; }\n"
            "var slides = root.querySelectorAll('.cw-ss-slide');\n"
            f"var durations = {durations_js};\n"
            f"var types = {types_js};\n"
            f"var transMs = {trans_ms_js};\n"
            "var TYPE = ['cut','fade','fade_black','dissolve','push','wipe','zoom'];\n"
            # Modes whose entry/exit states live in transform / clip-path,
            # so they need the transitions-off commit-then-flush dance.
            "var STAGED = {dissolve:1,push:1,wipe:1,zoom:1};\n"
            "var TX = ['cw-tx-dissolve','cw-tx-push','cw-tx-wipe','cw-tx-zoom',"
            "'cw-tx-incoming','cw-tx-outgoing','cw-ss-notrans'];\n"
            "function startVideo(slide) {\n"
            "  var v = slide.querySelector('video');\n"
            "  if (v) { try { v.currentTime = 0; var p = v.play();"
            " if (p && p.catch) { p.catch(function(){}); } } catch (e) {} }\n"
            "}\n"
            "if (slides.length >= 1) { startVideo(slides[0]); }\n"
            "if (slides.length <= 1) { return; }\n"
            "function clearTx(s) {\n"
            "  for (var i = 0; i < TX.length; i++) { s.classList.remove(TX[i]); }\n"
            "  s.style.removeProperty('--cw-ss-ms');\n"
            "}\n"
            "var idx = 0;\n"
            "function swapTo(n, prev) {\n"
            "  var incoming = slides[n];\n"
            "  var outgoing = (prev != null && prev !== n) ? slides[prev] : null;\n"
            "  var mode = TYPE[types[n]];\n"
            "  var durMs = (mode === 'cut') ? 0 : transMs[n];\n"
            "  for (var i = 0; i < slides.length; i++) { clearTx(slides[i]); }\n"
            "  incoming.style.setProperty('--cw-ss-ms', durMs + 'ms');\n"
            "  if (outgoing) { outgoing.style.setProperty('--cw-ss-ms', durMs + 'ms'); }\n"
            "  if (durMs === 0) {\n"
            "    incoming.classList.add('cw-ss-notrans');\n"
            "    if (outgoing) { outgoing.classList.add('cw-ss-notrans'); }\n"
            "  }\n"
            "  if (mode === 'fade_black' && durMs > 0 && outgoing) {\n"
            "    var half = Math.max(1, Math.floor(durMs / 2));\n"
            "    incoming.style.setProperty('--cw-ss-ms', half + 'ms');\n"
            "    outgoing.style.setProperty('--cw-ss-ms', half + 'ms');\n"
            "    outgoing.classList.remove('cw-ss-active');\n"
            "    setTimeout(function () { incoming.classList.add('cw-ss-active'); }, half);\n"
            "    startVideo(incoming);\n"
            "    return;\n"
            "  }\n"
            "  if (STAGED[mode]) {\n"
            "    var txClass = 'cw-tx-' + mode;\n"
            "    incoming.classList.add('cw-ss-notrans', txClass, 'cw-tx-incoming');\n"
            "    if (outgoing) { outgoing.classList.add('cw-ss-notrans', txClass, 'cw-tx-outgoing'); }\n"
            "    void root.offsetHeight;\n"
            "    incoming.classList.remove('cw-ss-notrans');\n"
            "    if (outgoing) { outgoing.classList.remove('cw-ss-notrans'); }\n"
            "  }\n"
            "  void root.offsetHeight;\n"
            "  for (var j = 0; j < slides.length; j++) {\n"
            "    slides[j].classList.toggle('cw-ss-active', j === n);\n"
            "  }\n"
            "  startVideo(incoming);\n"
            "  if (outgoing) {\n"
            "    var hide = outgoing;\n"
            "    setTimeout(function () {\n"
            "      if (!hide.classList.contains('cw-ss-active')) { clearTx(hide); }\n"
            "    }, Math.max(durMs + 100, 50));\n"
            "  }\n"
            "}\n"
            "function schedule() {\n"
            "  setTimeout(function () {\n"
            "    var prev = idx;\n"
            "    idx = (idx + 1) % slides.length;\n"
            "    swapTo(idx, prev);\n"
            "    schedule();\n"
            "  }, durations[idx]);\n"
            "}\n"
            "schedule();"
        )

        # The shared transition CSS carries the base ``.cw-ss-slide``
        # mechanics now, so it is ALWAYS emitted for any slideshow render
        # (even cut/fade-only or single-slide).  Identical bytes for every
        # instance, so the bundle builder de-dupes it to one <style> block
        # by content hash.  Selectors are intentionally global — see the
        # note on ``_SS_TRANSITION_CSS``.
        static_assets: list[WidgetStaticAsset] = [
            WidgetStaticAsset(
                kind="css",
                mime="text/css",
                content=_SS_TRANSITION_CSS,
            )
        ]

        return WidgetRender(
            html=html_out,
            css=css_out,
            init_js=init_js,
            referenced_asset_ids=referenced,
            static_assets=static_assets,
        )
