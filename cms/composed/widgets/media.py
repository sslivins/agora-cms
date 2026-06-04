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
