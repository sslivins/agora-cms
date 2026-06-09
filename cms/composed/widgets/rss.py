"""RSS widget — live headlines from an RSS/Atom feed.

Like the weather widget, this is one of the few widgets that makes a
**runtime network call** from the device.  It does NOT fetch the feed
directly, though: the bundle is served from the device's own local
shell HTTP server, so a cross-origin ``fetch()`` of a third-party feed
is browser CORS-blocked (most feeds send no
``Access-Control-Allow-Origin``).  Instead the bundle fetches the CMS's
own SSRF-guarded feed proxy (``GET /composed/rss``), which fetches and
parses the feed server-side and returns CORS-enabled JSON.

The proxy URL is baked in at build time.  On real hardware the device
needs the *absolute* CMS URL (a relative ``/composed/rss`` would hit
the device's local shell, not the CMS), so the widget reads
``BundleContext.cms_base_url``; for the same-origin CMS live preview /
headless thumbnail render (``cms_base_url is None``) it bakes a
relative same-origin URL instead.

Resilience mirrors the weather widget exactly:

* the ``fetch`` is wrapped in an ``AbortController`` timeout +
  ``try/catch`` so a network failure never throws — it falls back to a
  ``localStorage`` cache (keyed by a config fingerprint) and, failing
  that, a static "Headlines unavailable" message;
* the bundle's static markup contains **no** external ``src=`` /
  ``href=`` references — the proxy URL only ever exists as a JS string
  literal, preserving the bundle's no-external-reference invariant;
* feed titles are untrusted, so the runtime JS paints them with
  ``textContent`` (never ``innerHTML``) — there's no markup-injection
  surface.  No config-controlled string is interpolated into the
  emitted JavaScript.

Instance scoping: every DOM ID + CSS class is suffixed with the widget
instance UUID; the localStorage cache key is instance-scoped and
carries a ``{feed_url,item_count}`` fingerprint so changing the feed
never shows stale cached headlines.
"""

from __future__ import annotations

import html
import json
from typing import ClassVar
from urllib.parse import quote, urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator

from cms.composed.registry import BundleContext, Widget, WidgetRender
from cms.composed.schema import Cell

_HEX = r"^#[0-9a-fA-F]{6}$"

_FONT_STACKS: dict[str, str] = {
    "sans": "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif",
    "serif": "Georgia, Cambria, 'Times New Roman', serif",
    "mono": "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
}

# A neutral, well-known default so a freshly-dropped widget is a *valid*
# config (extra="forbid" + a required URL would otherwise make a
# just-placed widget fail save/publish before the user picks a feed).
_DEFAULT_FEED_URL = "https://feeds.bbci.co.uk/news/world/rss.xml"


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


class RssWidgetConfig(BaseModel):
    """User-editable config for :class:`RssWidget`."""

    model_config = ConfigDict(extra="forbid")

    feed_url: str = Field(default=_DEFAULT_FEED_URL, max_length=2048)
    heading: str = Field(default="", max_length=80)
    item_count: int = Field(default=5, ge=1, le=30)
    show_dates: bool = False
    color: str = Field(default="#ffffff", pattern=_HEX)
    font_family: str = Field(default="sans")
    font_size_px: int = Field(default=32, ge=8, le=256)
    # Feeds update at most a few times an hour; a 5-minute floor keeps
    # the proxy (and the upstream feed) from being hammered.
    refresh_seconds: int = Field(default=900, ge=300, le=86400)

    @field_validator("font_family")
    @classmethod
    def _font_in_allowlist(cls, v: str) -> str:
        if v not in _FONT_STACKS:
            allowed = ", ".join(sorted(_FONT_STACKS))
            raise ValueError(f"font_family must be one of: {allowed}")
        return v

    @field_validator("feed_url")
    @classmethod
    def _feed_url_http(cls, v: str) -> str:
        parsed = urlparse(v.strip())
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("feed_url must be an http(s) URL")
        return v.strip()


_RSS_INIT_JS_TEMPLATE = """
var root = document.getElementById($ROOT_ID_LIT);
var listEl = document.getElementById($LIST_ID_LIT);
var FEED_URL = $URL_LIT;
var CFG_FP = $CFG_FP_LIT;
var CACHE_KEY = $CACHE_KEY_LIT;
var REFRESH_MS = $REFRESH_MS;
var SHOW_DATES = $SHOW_DATES;
var ITEM_CLASS = $ITEM_CLASS_LIT;
var DATE_CLASS = $DATE_CLASS_LIT;
function lsGet() {
  try {
    var raw = localStorage.getItem(CACHE_KEY);
    if (!raw) { return null; }
    var o = JSON.parse(raw);
    if (!o || o.fp !== CFG_FP) { return null; }
    return o.items || null;
  } catch (e) { return null; }
}
function lsSet(items) {
  try {
    localStorage.setItem(CACHE_KEY, JSON.stringify({fp: CFG_FP, items: items}));
  } catch (e) {}
}
function clearList() {
  if (!listEl) { return; }
  while (listEl.firstChild) { listEl.removeChild(listEl.firstChild); }
}
function paint(items) {
  if (!listEl) { return; }
  clearList();
  if (!items || !items.length) {
    var none = document.createElement('div');
    none.className = ITEM_CLASS;
    none.textContent = 'Headlines unavailable';
    listEl.appendChild(none);
    return;
  }
  for (var i = 0; i < items.length; i++) {
    var it = items[i] || {};
    var row = document.createElement('div');
    row.className = ITEM_CLASS;
    var title = document.createElement('div');
    title.textContent = it.title || '';
    row.appendChild(title);
    if (SHOW_DATES && it.pubDate) {
      var d = document.createElement('div');
      d.className = DATE_CLASS;
      d.textContent = it.pubDate;
      row.appendChild(d);
    }
    listEl.appendChild(row);
  }
}
function fallback() {
  var c = lsGet();
  if (c) { paint(c); return; }
  paint(null);
}
function schedule() {
  if (!root || !document.body.contains(root)) { return; }
  setTimeout(refresh, REFRESH_MS + Math.floor(Math.random() * 30000));
}
function refresh() {
  if (!root || !document.body.contains(root)) { return; }
  var ctrl = new AbortController();
  var to = setTimeout(function () { ctrl.abort(); }, 8000);
  fetch(FEED_URL, {signal: ctrl.signal})
    .then(function (r) { if (!r.ok) { throw new Error('http ' + r.status); } return r.json(); })
    .then(function (d) {
      var items = d && d.items ? d.items : null;
      if (!items) { throw new Error('bad shape'); }
      paint(items);
      lsSet(items);
    })
    .catch(function () { fallback(); })
    .then(function () { clearTimeout(to); schedule(); });
}
fallback();
refresh();
""".strip()


def _proxy_url(base: str | None, feed_url: str, count: int) -> str:
    """Build the CMS feed-proxy URL the device fetches at runtime.

    Absolute when ``base`` is set (real device bundle); relative
    same-origin otherwise (CMS preview / thumbnail render).
    """
    prefix = base.rstrip("/") if base else ""
    return f"{prefix}/composed/rss?url={quote(feed_url, safe='')}&count={count}"


def _build_rss_init_js(
    *,
    root_id: str,
    list_id: str,
    url: str,
    cfg_fp: str,
    cache_key: str,
    refresh_ms: int,
    show_dates: bool,
    item_class: str,
    date_class: str,
) -> str:
    return (
        _RSS_INIT_JS_TEMPLATE.replace("$ROOT_ID_LIT", _js_str(root_id))
        .replace("$LIST_ID_LIT", _js_str(list_id))
        .replace("$URL_LIT", _js_str(url))
        .replace("$CFG_FP_LIT", _js_str(cfg_fp))
        .replace("$CACHE_KEY_LIT", _js_str(cache_key))
        .replace("$REFRESH_MS", str(refresh_ms))
        .replace("$SHOW_DATES", "true" if show_dates else "false")
        .replace("$ITEM_CLASS_LIT", _js_str(item_class))
        .replace("$DATE_CLASS_LIT", _js_str(date_class))
    )


class RssWidget(Widget):
    """Live RSS/Atom headlines (fetched via the CMS feed proxy)."""

    slug: ClassVar[str] = "rss"
    display_name: ClassVar[str] = "RSS Headlines"
    icon: ClassVar[str] = "\U0001f4f0"  # newspaper
    ConfigSchema: ClassVar[type[BaseModel]] = RssWidgetConfig
    config_version: ClassVar[int] = 1

    def default_config(self) -> dict:
        return {
            "feed_url": _DEFAULT_FEED_URL,
            "heading": "",
            "item_count": 5,
            "show_dates": False,
            "color": "#ffffff",
            "font_family": "sans",
            "font_size_px": 32,
            "refresh_seconds": 900,
        }

    def editor_template(self) -> str:
        return "composed/widgets/rss.html"

    def validate_semantic(self, config: BaseModel) -> list[str]:
        assert isinstance(config, RssWidgetConfig)
        return []

    def render_html(
        self,
        config: BaseModel,
        cell: Cell,
        instance_id: str,
        ctx: BundleContext | None = None,
    ) -> WidgetRender:
        assert isinstance(config, RssWidgetConfig), (
            "RssWidget.render_html expects a RssWidgetConfig instance"
        )

        css_class = f"cw-rss-{instance_id}"
        root_id = f"cw-rss-root-{instance_id}"
        list_id = f"cw-rss-list-{instance_id}"
        item_class = f"{css_class}-item"
        date_class = f"{css_class}-date"
        font_stack = _FONT_STACKS[config.font_family]

        # Heading is the ONLY config-controlled string in the markup —
        # escaped here, never passed into JS. Feed titles are painted at
        # runtime via textContent.
        heading_html = (
            f'<div class="{css_class}-heading">'
            f"{html.escape(config.heading)}</div>"
            if config.heading.strip()
            else ""
        )
        html_out = (
            f'<div id="{root_id}" class="{css_class}">'
            f"{heading_html}"
            f'<div id="{list_id}" class="{css_class}-list">'
            f'<div class="{item_class}">Loading headlines\u2026</div>'
            f"</div>"
            f"</div>"
        )

        heading_size = max(8, int(config.font_size_px * 1.1))
        date_size = max(8, int(config.font_size_px * 0.6))
        css_out = (
            f".{css_class} {{\n"
            f"  width: 100%;\n"
            f"  height: 100%;\n"
            f"  display: flex;\n"
            f"  flex-direction: column;\n"
            f"  color: {config.color};\n"
            f"  font-family: {font_stack};\n"
            f"  overflow: hidden;\n"
            f"  box-sizing: border-box;\n"
            f"}}\n"
            f".{css_class}-heading {{\n"
            f"  font-size: {heading_size}px;\n"
            f"  font-weight: 700;\n"
            f"  margin-bottom: 0.3em;\n"
            f"  flex: 0 0 auto;\n"
            f"}}\n"
            f".{css_class}-list {{\n"
            f"  flex: 1 1 auto;\n"
            f"  overflow: hidden;\n"
            f"  display: flex;\n"
            f"  flex-direction: column;\n"
            f"  gap: 0.4em;\n"
            f"}}\n"
            f".{css_class}-item {{\n"
            f"  font-size: {config.font_size_px}px;\n"
            f"  line-height: 1.2;\n"
            f"}}\n"
            f".{css_class}-date {{\n"
            f"  font-size: {date_size}px;\n"
            f"  opacity: 0.7;\n"
            f"  margin-top: 0.1em;\n"
            f"}}"
        )

        base = ctx.cms_base_url if ctx else None
        cfg_fp = f"{config.feed_url}|{config.item_count}"
        init_js = _build_rss_init_js(
            root_id=root_id,
            list_id=list_id,
            url=_proxy_url(base, config.feed_url, config.item_count),
            cfg_fp=cfg_fp,
            cache_key=f"cw-rss-{instance_id}",
            refresh_ms=config.refresh_seconds * 1000,
            show_dates=config.show_dates,
            item_class=item_class,
            date_class=date_class,
        )

        return WidgetRender(html=html_out, css=css_out, init_js=init_js)
