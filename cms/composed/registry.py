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

    # ── Mandatory overrides ──────────────────────────────────────
    def render_html(
        self,
        config: BaseModel,
        cell: Cell,
        instance_id: str,
    ) -> WidgetRender:
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
