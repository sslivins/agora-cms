"""Widget plugin contract + global registry.

Every widget lives in ``cms/composed/widgets/<slug>.py`` and subclasses
:class:`Widget`.  The registry discovers all widgets at import time
(Phase 1B wires the auto-import; for Phase 0 only the abstract base
and an explicit ``register`` API ship).

See ``plan.md`` for the full design.  The non-obvious rules:

* Every widget must scope its DOM IDs and CSS classes to
  ``instance_id``.  The bundle builder will fail-loud if a widget
  emits a non-scoped selector for a class it also emits in ``css``.
* ``WidgetRender.referenced_asset_ids`` enables stale-bundle detection
  (the builder records the union of these alongside ``bundle_built_at``).
* ``migrate_config`` defaults to identity.  Bump ``config_version`` on
  any breaking change to the widget's config shape and provide a
  migration so older saved layouts continue to load.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import ClassVar, Literal

from pydantic import BaseModel

from cms.composed.schema import Cell


@dataclass(frozen=True)
class SlideshowSlidePlan:
    """One resolved slide of a SLIDESHOW asset embedded in a media widget.

    When a :class:`~cms.composed.widgets.media.MediaWidget` points its
    ``asset_id`` at a SLIDESHOW asset, the publish / render layer
    re-resolves the slideshow's ordered slides at build time (so the
    composed bundle always tracks the *live* slideshow) and hands the
    bundle builder one of these per slide, in ``position`` order.

    ``source_asset_id`` is the per-slide IMAGE/VIDEO source — its bytes
    (image) or sibling/data URL (video) are routed into the same
    :class:`BundleContext` channels a standalone media asset would use.
    ``duration_ms`` is how long the slide shows before advancing.
    ``transition`` is the slide's entrance transition — the composed
    media cell renders the full slideshow set as self-contained CSS/JS
    (``cut`` / ``fade`` / ``fade_black`` / ``dissolve`` / ``push`` /
    ``wipe`` / ``zoom``).  ``transition_ms`` is the transition duration
    (ignored for ``"cut"``).  ``fit`` is the per-slide CSS object-fit
    (``cover`` / ``contain``); ``effect`` is the per-slide motion effect
    (``none`` / ``ken_burns``) — both mirror the on-device manifest
    schema 1.3 fields so an embedded slideshow reads the same as the
    standalone one.
    """

    source_asset_id: uuid.UUID
    duration_ms: int
    transition: str = "cut"
    transition_ms: int = 0
    fit: str = "cover"
    effect: str = "none"


@dataclass
class BundleContext:
    """Per-build context passed into every widget's ``render_html``.

    The bundle builder pre-fetches all bytes a layout needs (via the
    asset loader supplied to :func:`cms.composed.bundle.build_bundle`)
    and surfaces them here so widgets stay pure — they don't open files,
    don't hit the DB, don't talk to the network.

    ``asset_bytes`` and ``asset_mimes`` are keyed by the
    ``uuid.UUID`` IDs the widget declared via
    :meth:`Widget.declared_asset_ids`.  A widget that references an
    asset MUST also declare it; the bundle builder rejects layouts
    whose widgets reference undeclared assets at render time.

    ``sibling_asset_urls`` is the parallel channel for assets the
    bundle should *reference by URL* rather than inline as bytes.
    Used for VIDEO assets in Phase 1C onward — videos are way too big
    to inline as data URIs, so the publish layer registers them as
    sibling assets on the device cache and the bundle's ``<video>``
    tag just points at the device-local URL (e.g.
    ``/assets/videos/foo.mp4``).  Keyed by the same declared asset ID.

    A given asset ID appears in *either* the bytes channel *or* the
    sibling-URLs channel, never both — the publish layer buckets by
    ``asset_type``.

    ``cms_base_url`` is the canonical public base URL of the CMS
    (scheme + host, no trailing slash), or ``None``.  It exists for
    widgets that bake an *absolute* call-back URL into the device
    bundle — e.g. the RSS widget points the device's runtime fetch at
    the CMS's ``/composed/rss`` feed proxy.  The device serves the
    bundle from its own local shell HTTP server, so a relative URL
    would hit the device, not the CMS; an absolute URL is required on
    real hardware.  ``None`` means "bake a relative URL" — correct for
    the same-origin CMS live preview (where the document is served by
    the CMS itself) and for the headless thumbnail render.  Widgets
    that make no runtime CMS call ignore this entirely.

    ``slideshow_plans`` is the additive channel for a media widget whose
    ``asset_id`` points at a SLIDESHOW asset.  It maps that container
    asset ID to the ordered list of :class:`SlideshowSlidePlan` the
    publish / render layer resolved from the live slideshow.  The
    per-slide *source* asset IDs are routed into ``asset_bytes`` /
    ``sibling_asset_urls`` exactly like standalone media; the plan adds
    the ordering, per-slide duration, and transition metadata the media
    widget needs to emit its client-side cycling markup.  Empty for
    every layout that contains no slideshow-backed media widget — in
    which case the whole build is byte-for-byte unchanged.

    Empty defaults are intentional: trivial widgets (text, clock) that
    never touch assets can ignore the parameter entirely.
    """

    asset_bytes: dict[uuid.UUID, bytes] = field(default_factory=dict)
    asset_mimes: dict[uuid.UUID, str] = field(default_factory=dict)
    sibling_asset_urls: dict[uuid.UUID, str] = field(default_factory=dict)
    cms_base_url: str | None = None
    slideshow_plans: dict[uuid.UUID, list[SlideshowSlidePlan]] = field(
        default_factory=dict
    )


@dataclass
class WidgetStaticAsset:
    """A binary or text asset the widget needs in the bundle.

    The bundle builder is responsible for inlining these (data URIs
    for binary content; embedded ``<style>`` / ``<script>`` blocks
    for text content).  De-duped across instances by content hash.
    """

    kind: Literal["font", "image", "js", "css"]
    content: bytes | str
    mime: str


@dataclass
class WidgetRender:
    """Result of a widget's ``render_html`` call.

    Returned to the bundle builder, which assembles the final
    single-file HTML.  Instance-scoping is the widget's responsibility
    (see module docstring).
    """

    html: str
    css: str = ""
    js: str = ""
    init_js: str | None = None
    static_assets: list[WidgetStaticAsset] = field(default_factory=list)
    referenced_asset_ids: list[uuid.UUID] = field(default_factory=list)


class Widget:
    """Base class every widget plugin extends.

    Subclasses MUST set ``slug``, ``display_name``, ``icon``, and
    ``ConfigSchema``.  ``config_version`` defaults to 1; bump it on
    breaking config changes and supply ``migrate_config``.

    All instance methods receive validated config + a stable
    ``instance_id``.  Widgets must scope DOM/CSS to that ID — see the
    "Instance scoping rule" in plan.md.
    """

    # Subclasses override these as plain class attributes.
    slug: ClassVar[str]
    display_name: ClassVar[str]
    icon: ClassVar[str]
    ConfigSchema: ClassVar[type[BaseModel]]
    config_version: ClassVar[int] = 1
    # Legacy/internal widgets that should not be offered to the AI
    # assistant as a placeable type. The publish/render path still
    # supports them (for slides that already use them), but the
    # assistant must not mint new instances. Defaults to visible.
    assistant_hidden: ClassVar[bool] = False

    # ── Mandatory overrides ──────────────────────────────────────
    def render_html(
        self,
        config: BaseModel,
        cell: Cell,
        instance_id: str,
        ctx: BundleContext | None = None,
    ) -> WidgetRender:
        """Render a single widget instance.

        ``ctx`` is the per-build :class:`BundleContext` populated by
        the bundle builder.  Widgets that don't need pre-fetched
        bytes can ignore it; widgets that do (image, video) read
        from ``ctx.asset_bytes`` / ``ctx.asset_mimes`` keyed by the
        IDs they returned from :meth:`declared_asset_ids`.

        The parameter defaults to ``None`` so unit tests and trivial
        widgets can call ``render_html(cfg, cell, "id")`` directly;
        production calls from the bundle builder always pass a real
        :class:`BundleContext`.
        """
        raise NotImplementedError

    def editor_template(self) -> str:
        """Path (relative to the Jinja loader) to the settings panel."""
        raise NotImplementedError

    def default_config(self) -> dict:
        """Config dict for a newly-dropped widget.

        Must validate against ``ConfigSchema``.
        """
        raise NotImplementedError

    # ── Optional overrides ───────────────────────────────────────
    def migrate_config(self, raw: dict, from_version: int) -> dict:
        """Upgrade an older config dict to ``config_version``.

        Default is identity (safe for additive changes guarded by
        ``Optional`` fields with sensible defaults).  Override on
        renames, removals, or required-field additions.
        """
        return raw

    def declared_asset_ids(self, config: BaseModel) -> list[uuid.UUID]:
        """Asset IDs this widget instance will reference at render time.

        Returning an ID here means the bundle builder will pre-fetch
        the asset's bytes + MIME and stash them in :class:`BundleContext`
        before calling :meth:`render_html`.  Used both to populate the
        render context AND as the canonical staleness-tracking input
        (the bundle records the union of declared IDs as
        ``bundle_source_asset_ids``).

        Default: no asset dependencies.

        IMPORTANT: a widget that emits ``referenced_asset_ids`` in its
        :class:`WidgetRender` MUST also have declared those same IDs
        here.  The bundle builder asserts this invariant so it can't be
        silently broken by a copy-paste bug.
        """
        return []

    def validate_semantic(self, config: BaseModel) -> list[str]:
        """Cross-field / external checks beyond Pydantic shape.

        Examples: a referenced asset ID exists, a referenced asset is
        of the expected type, an URL matches the allowlist.  Return a
        list of human-readable error messages; empty list = ok.

        Default: no extra checks.
        """
        return []


class WidgetRegistry:
    """In-process registry of available widget plugins.

    The default :func:`get_registry` returns a process-wide singleton.
    Tests can construct a fresh registry to isolate plugin sets.
    """

    def __init__(self) -> None:
        self._widgets: dict[str, Widget] = {}

    def register(self, widget: Widget) -> None:
        slug = widget.slug
        if not slug:
            raise ValueError("widget.slug is required")
        if slug in self._widgets:
            raise ValueError(f"widget slug already registered: {slug!r}")
        self._widgets[slug] = widget

    def get(self, slug: str) -> Widget | None:
        return self._widgets.get(slug)

    def has(self, slug: str) -> bool:
        return slug in self._widgets

    def slugs(self) -> list[str]:
        return sorted(self._widgets.keys())

    def all(self) -> list[Widget]:
        return [self._widgets[s] for s in self.slugs()]


# Process-wide registry.  Phase 1A will populate it with the trivial
# text widget; Phase 1B fills it out.  Phase 0 ships empty so that
# all behaviour (including "unknown widget type" rejection) is
# testable without committing to any specific widget implementations.
_REGISTRY = WidgetRegistry()


def get_registry() -> WidgetRegistry:
    return _REGISTRY
