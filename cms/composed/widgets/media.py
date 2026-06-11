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

from cms.composed.registry import BundleContext, Widget, WidgetRender
from cms.composed.schema import Cell


log = logging.getLogger(__name__)


# Soft warning threshold for inlined image size.  Matches the
# ImageWidget threshold — we hit this on a re-uploaded 4K photo that
# someone dropped into a 200px cell.
_LARGE_IMAGE_WARN_BYTES: int = 2 * 1024 * 1024


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
        baked-in timer toggles the active class to advance; CSS opacity
        transitions handle the cut / cross-fade.
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

        for idx, plan in enumerate(plans):
            source = plan.source_asset_id
            referenced.append(source)
            durations.append(int(plan.duration_ms))

            active = " cw-ss-active" if idx == 0 else ""
            # Per-slide transition duration overrides the container's
            # default; a "cut" slide is baked as 0ms (instant swap).
            trans_ms = int(plan.transition_ms) if plan.transition == "fade" else 0
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

            slide_html.append(
                f'<div class="cw-ss-slide{active}" style="{slide_style}">'
                f"{inner}</div>"
            )

        html_out = f'<div class="{css_class}">' + "".join(slide_html) + "</div>"

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
            f"  transition-property: opacity;\n"
            f"  transition-timing-function: ease-in-out;\n"
            f"}}\n"
            f".{css_class} .cw-ss-slide.cw-ss-active {{\n"
            f"  opacity: 1;\n"
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

        # Bake ONLY integer durations into the runtime — never
        # interpolate raw config strings into JS.
        durations_js = "[" + ",".join(str(d) for d in durations) + "]"
        init_js = (
            "var root = document.querySelector('.cw-ss-' + instanceId);\n"
            "if (!root) { return; }\n"
            "var slides = root.querySelectorAll('.cw-ss-slide');\n"
            f"var durations = {durations_js};\n"
            "function startVideo(slide) {\n"
            "  var v = slide.querySelector('video');\n"
            "  if (v) { try { v.currentTime = 0; var p = v.play();"
            " if (p && p.catch) { p.catch(function(){}); } } catch (e) {} }\n"
            "}\n"
            "if (slides.length >= 1) { startVideo(slides[0]); }\n"
            "if (slides.length <= 1) { return; }\n"
            "var idx = 0;\n"
            "function activate(n) {\n"
            "  for (var i = 0; i < slides.length; i++) {\n"
            "    slides[i].classList.toggle('cw-ss-active', i === n);\n"
            "  }\n"
            "  startVideo(slides[n]);\n"
            "}\n"
            "function schedule() {\n"
            "  setTimeout(function () {\n"
            "    idx = (idx + 1) % slides.length;\n"
            "    activate(idx);\n"
            "    schedule();\n"
            "  }, durations[idx]);\n"
            "}\n"
            "schedule();"
        )

        return WidgetRender(
            html=html_out,
            css=css_out,
            init_js=init_js,
            referenced_asset_ids=referenced,
        )
