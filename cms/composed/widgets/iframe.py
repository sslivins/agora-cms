"""Web / iframe embed widget — displays a live web page inside a cell.

This is one of the few widgets that surfaces *live* third-party content
on the device.  It renders an ``<iframe>`` pointed at a configured URL.

The hard constraint it must respect is the bundle's
**no-external-reference invariant** (enforced by
``tests/test_composed_bundle.py::TestNoExternalReferences``): the
single-file bundle may not contain any ``src=`` / ``href=`` attribute
referencing an ``http(s):`` / ``//`` origin.  A naive
``<iframe src="https://…">`` would fail that test outright.

The sanctioned escape hatch (the same one the weather and RSS widgets
use) is to keep the URL **only as a JavaScript string literal** and
assign it to ``iframe.src`` at runtime, after the document has loaded.
The static markup ships an ``<iframe>`` with **no** ``src`` attribute,
so the invariant holds; ``init_js`` sets ``frame.src = URL`` on the
device.

Unlike the weather / RSS widgets, an iframe needs **no CMS proxy**:
frame *navigation* is not subject to CORS (only ``fetch``/XHR is), so
the device's browser can load the URL directly.  The only thing that
can block it is the target site's own ``X-Frame-Options`` /
``Content-Security-Policy: frame-ancestors`` — which is the target's
choice and outside our control.  Because of that, no
``BundleContext.cms_base_url`` and no server route are involved.

Resilience / offline tolerance:

* The iframe has a **transparent** background and sits on top of a
  fallback overlay (``z-index``) that shows a configurable
  "unavailable" message painted with the configured background colour.
  When the page loads, its content paints over the overlay; when the
  device is offline or the target refuses framing (blank/transparent
  frame), the overlay shows through.  This is best-effort — we cannot
  read cross-origin frame state, so a target that renders its own
  error page will hide the overlay.
* Optional periodic reload (``refresh_seconds``) keeps live dashboards
  fresh; ``0`` disables auto-reload.

Security:

* The frame is ``sandbox``-ed.  ``allow-same-origin`` is always set so
  the framed page can run normally; ``allow-scripts`` is opt-in via
  ``allow_scripts`` (default on — most embeds are dashboards that need
  JS).  We never grant ``allow-top-navigation`` / ``allow-popups`` /
  ``allow-forms``, so a framed page can't hijack the slide, spawn
  windows, or submit forms.
* ``referrerpolicy="no-referrer"`` avoids leaking the device URL.
* The URL is validated to ``http(s)`` (no ``javascript:`` / ``data:``
  top-level navigation) and is the only config string that reaches the
  emitted JavaScript — and only via ``_js_str`` escaping so a stray
  ``</script>`` can't break out.

Instance scoping: every DOM ID + CSS class is suffixed with the widget
instance UUID.
"""

from __future__ import annotations

import html
import json
from typing import ClassVar
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator

from cms.composed.registry import BundleContext, Widget, WidgetRender
from cms.composed.schema import Cell

_HEX = r"^#[0-9a-fA-F]{6}$"

# A neutral, well-known default so a freshly-dropped widget is a *valid*
# config (extra="forbid" + a required URL would otherwise make a
# just-placed widget fail save/publish before the user picks a page).
_DEFAULT_URL = "https://example.com/"


def _js_str(s: str) -> str:
    """Serialise a Python string as a JS string literal, HTML-safe.

    ``json.dumps`` handles quote/backslash/control-char escaping; we
    additionally escape ``<>&`` so a stray ``</script>`` can never
    terminate the embedded script block.
    """
    return (
        json.dumps(s)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


class IframeWidgetConfig(BaseModel):
    """User-editable config for :class:`IframeWidget`."""

    model_config = ConfigDict(extra="forbid")

    url: str = Field(default=_DEFAULT_URL, max_length=2048)
    allow_scripts: bool = True
    # 0 disables auto-reload; otherwise a 60-second floor keeps a busy
    # dashboard from being reloaded into the ground.
    refresh_seconds: int = Field(default=0, ge=0, le=86400)
    background_color: str = Field(default="#000000", pattern=_HEX)
    unavailable_text: str = Field(
        default="Content unavailable", max_length=120
    )

    @field_validator("url")
    @classmethod
    def _url_http(cls, v: str) -> str:
        parsed = urlparse(v.strip())
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("url must be an http(s) URL")
        return v.strip()

    @field_validator("refresh_seconds")
    @classmethod
    def _refresh_floor(cls, v: int) -> int:
        if v != 0 and v < 60:
            raise ValueError("refresh_seconds must be 0 (off) or >= 60")
        return v


_IFRAME_INIT_JS_TEMPLATE = """
var frame = document.getElementById($FRAME_ID_LIT);
var overlay = document.getElementById($OVERLAY_ID_LIT);
var URL = $URL_LIT;
var REFRESH_MS = $REFRESH_MS;
if (frame) {
  frame.addEventListener("load", function () {
    // Frame painted something; hide the offline/blocked fallback.
    if (overlay) { overlay.style.display = "none"; }
  });
  try {
    frame.src = URL;
  } catch (e) {
    if (overlay) { overlay.style.display = "flex"; }
  }
  if (REFRESH_MS > 0) {
    setInterval(function () {
      try {
        // Re-assign src to force a fresh load (live dashboards).
        frame.src = URL;
      } catch (e) {}
    }, REFRESH_MS);
  }
}
"""


def _build_iframe_init_js(
    *,
    frame_id: str,
    overlay_id: str,
    url: str,
    refresh_ms: int,
) -> str:
    """Build the per-instance init JS via literal substitution.

    Uses ``.replace`` (not f-strings / ``.format``) so config-derived
    values can only ever land in the explicit ``$TOKEN`` slots — there
    is no way for a config string to be interpreted as part of the JS
    template itself.
    """
    return (
        _IFRAME_INIT_JS_TEMPLATE.replace("$FRAME_ID_LIT", _js_str(frame_id))
        .replace("$OVERLAY_ID_LIT", _js_str(overlay_id))
        .replace("$URL_LIT", _js_str(url))
        .replace("$REFRESH_MS", str(int(refresh_ms)))
    )


class IframeWidget(Widget):
    """Embed a live web page in a cell via a runtime-injected iframe."""

    slug: ClassVar[str] = "iframe"
    display_name: ClassVar[str] = "Web Embed"
    icon: ClassVar[str] = "\U0001f310"  # 🌐
    ConfigSchema: ClassVar[type[BaseModel]] = IframeWidgetConfig
    config_version: ClassVar[int] = 1

    def default_config(self) -> dict:
        return {
            "url": _DEFAULT_URL,
            "allow_scripts": True,
            "refresh_seconds": 0,
            "background_color": "#000000",
            "unavailable_text": "Content unavailable",
        }

    def editor_template(self) -> str:
        return "composed/widgets/iframe.html"

    def render_html(
        self,
        config: BaseModel,
        cell: Cell,
        instance_id: str,
        ctx: BundleContext | None = None,
    ) -> WidgetRender:
        del ctx  # iframe loads at runtime; no build-time asset deps
        assert isinstance(config, IframeWidgetConfig), (
            "IframeWidget.render_html expects an IframeWidgetConfig instance"
        )

        css_class = f"cw-iframe-{instance_id}"
        root_id = f"cw-iframe-root-{instance_id}"
        frame_id = f"cw-iframe-frame-{instance_id}"
        overlay_id = f"cw-iframe-overlay-{instance_id}"

        # Minimal sandbox: same-origin always (so the framed page runs
        # normally); scripts opt-in.  Never top-navigation/popups/forms.
        sandbox_tokens = ["allow-same-origin"]
        if config.allow_scripts:
            sandbox_tokens.append("allow-scripts")
        sandbox_attr = " ".join(sandbox_tokens)

        # unavailable_text is the ONLY config-controlled string in the
        # markup — escaped here, never passed into JS.
        unavailable_html = html.escape(config.unavailable_text)

        # NOTE: the <iframe> deliberately ships with NO src attribute.
        # init_js assigns frame.src at runtime so the bundle's
        # no-external-reference invariant holds.
        html_out = (
            f'<div id="{root_id}" class="{css_class}">'
            f'<div id="{overlay_id}" class="{css_class}-overlay">'
            f"{unavailable_html}</div>"
            f'<iframe id="{frame_id}" class="{css_class}-frame" '
            f'sandbox="{sandbox_attr}" referrerpolicy="no-referrer" '
            f'title="{unavailable_html}" loading="lazy"></iframe>'
            f"</div>"
        )

        css_out = (
            f".{css_class} {{\n"
            f"  position: relative;\n"
            f"  width: 100%;\n"
            f"  height: 100%;\n"
            f"  overflow: hidden;\n"
            f"  background: {config.background_color};\n"
            f"}}\n"
            f".{css_class}-overlay {{\n"
            f"  position: absolute;\n"
            f"  inset: 0;\n"
            f"  z-index: 0;\n"
            f"  display: flex;\n"
            f"  align-items: center;\n"
            f"  justify-content: center;\n"
            f"  text-align: center;\n"
            f"  padding: 0.5em;\n"
            f"  color: rgba(255, 255, 255, 0.7);\n"
            f"  font-family: system-ui, -apple-system, 'Segoe UI', "
            f"Roboto, sans-serif;\n"
            f"  font-size: 24px;\n"
            f"  background: {config.background_color};\n"
            f"}}\n"
            f".{css_class}-frame {{\n"
            f"  position: absolute;\n"
            f"  inset: 0;\n"
            f"  z-index: 1;\n"
            f"  width: 100%;\n"
            f"  height: 100%;\n"
            f"  border: 0;\n"
            f"  background: transparent;\n"
            f"}}"
        )

        init_js = _build_iframe_init_js(
            frame_id=frame_id,
            overlay_id=overlay_id,
            url=config.url,
            refresh_ms=config.refresh_seconds * 1000,
        )

        return WidgetRender(html=html_out, css=css_out, init_js=init_js)
