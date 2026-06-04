"""Load-time migration of composed-slide layout dicts.

A persisted layout records, per widget instance, the ``config_version``
the config dict was authored against.  When a widget's
``config_version`` is bumped (breaking-change scenarios — renamed keys,
removed fields, restructured nested objects), old persisted configs
need to be upgraded before they can be revalidated against the current
``ConfigSchema``.

This module owns that upgrade path.  Callers run :func:`load_and_migrate`
on a raw layout dict (typically straight out of
``composed_slides.layout_json``); it returns a fully-validated
:class:`~cms.composed.schema.Layout` plus a flag indicating whether
anything was actually migrated.  When the flag is ``True``, callers are
expected to persist the upgraded dict via their normal save path so the
migration runs only once.

Design choices:

* Migration is a **side-effect-free function**.  No DB calls, no IO.
  Persistence is the caller's responsibility — this keeps it trivially
  unit-testable and lets it run in places where there is no DB session
  (eg. the bundle preview path).
* Out-of-version configs are upgraded one major version at a time via
  the widget's :meth:`~cms.composed.registry.Widget.migrate_config`
  hook.  We loop ``while config_version < widget.config_version`` so a
  single load can step from v1 → v4 if needed.
* Forward-version configs (``config_version > widget.config_version``)
  raise.  There is no automatic downgrade; if a deployed CMS sees a
  newer schema it must be upgraded too.
* Unknown widget types raise.  A layout that references a removed
  widget cannot be silently dropped — the device would render an empty
  cell where content used to be.
* Layout ``schema_version`` is checked against the current
  :data:`~cms.composed.schema.SCHEMA_VERSION` constant; mismatch raises.
  Per-version migrations of the *layout* shell (vs. per-widget configs)
  are deferred — none have been needed yet.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from cms.composed.registry import get_registry
from cms.composed.schema import SCHEMA_VERSION, Layout


class LayoutMigrationError(Exception):
    """Raised when a layout dict cannot be loaded or migrated."""


class UnsupportedSchemaVersionError(LayoutMigrationError):
    """Raised when ``schema_version`` is not the current value.

    There is no automatic layout-shell migration yet; if a layout
    persists at an older or newer version than the running CMS, that
    is a deployment-state bug the caller must surface.
    """


class UnknownWidgetTypeError(LayoutMigrationError):
    """Raised when a layout references a widget slug not in the registry.

    Most commonly this means the widget was removed from the codebase
    after layouts referencing it were persisted.  The caller must
    decide how to surface that — there is no safe automatic recovery.
    """


class WidgetConfigForwardVersionError(LayoutMigrationError):
    """Raised when a widget instance's ``config_version`` is newer than
    the registered widget.

    Indicates a deployment where the CMS is older than the data it is
    being asked to read; the operator must upgrade the CMS image.
    """


def load_and_migrate(raw: dict[str, Any]) -> tuple[Layout, bool]:
    """Load and (if necessary) migrate a raw layout dict.

    Returns a ``(layout, migrated)`` tuple.  When ``migrated`` is
    ``True``, the caller should persist the resulting layout via
    :meth:`Layout.model_dump` so the migration runs only once.

    Raises:
        UnsupportedSchemaVersionError: if the layout's ``schema_version``
            differs from the current :data:`SCHEMA_VERSION`.
        UnknownWidgetTypeError: if any widget references a slug that is
            not currently registered.
        WidgetConfigForwardVersionError: if any widget's
            ``config_version`` is newer than the running widget's.
        LayoutMigrationError: if a widget's ``migrate_config`` returns a
            dict that fails its current ``ConfigSchema`` validation.
    """
    if not isinstance(raw, dict):
        raise LayoutMigrationError(
            f"layout payload must be a dict, got {type(raw).__name__}"
        )

    layout_schema_version = raw.get("schema_version", SCHEMA_VERSION)
    if layout_schema_version != SCHEMA_VERSION:
        raise UnsupportedSchemaVersionError(
            f"layout schema_version {layout_schema_version!r} is not the "
            f"current version {SCHEMA_VERSION}; no migration path defined"
        )

    registry = get_registry()
    migrated = False

    widgets = raw.get("widgets", [])
    if not isinstance(widgets, list):
        raise LayoutMigrationError(
            f"layout.widgets must be a list, got {type(widgets).__name__}"
        )

    for widget_entry in widgets:
        if not isinstance(widget_entry, dict):
            raise LayoutMigrationError(
                "every entry in layout.widgets must be a dict; "
                f"got {type(widget_entry).__name__}"
            )

        slug = widget_entry.get("type")
        if not slug:
            raise LayoutMigrationError(
                "widget entry missing required 'type' field"
            )

        widget = registry.get(slug)
        if widget is None:
            raise UnknownWidgetTypeError(
                f"layout references unknown widget type {slug!r}; "
                "was the widget removed from this CMS?"
            )

        current_version = int(widget_entry.get("config_version", 1))
        target_version = widget.config_version

        if current_version > target_version:
            raise WidgetConfigForwardVersionError(
                f"widget {slug!r} instance is at config_version "
                f"{current_version} but the running widget is at "
                f"{target_version}; CMS is older than the persisted data"
            )

        if current_version < target_version:
            config = widget_entry.get("config", {})
            if not isinstance(config, dict):
                raise LayoutMigrationError(
                    f"widget {slug!r} config must be a dict; "
                    f"got {type(config).__name__}"
                )
            stepped = config
            for from_version in range(current_version, target_version):
                stepped = widget.migrate_config(stepped, from_version)
                if not isinstance(stepped, dict):
                    raise LayoutMigrationError(
                        f"widget {slug!r} migrate_config(from_version="
                        f"{from_version}) returned non-dict "
                        f"{type(stepped).__name__}"
                    )
            widget_entry["config"] = stepped
            widget_entry["config_version"] = target_version
            migrated = True

        try:
            widget.ConfigSchema.model_validate(widget_entry["config"])
        except ValidationError as exc:
            raise LayoutMigrationError(
                f"widget {slug!r} config failed validation after "
                f"migration to v{target_version}: {exc}"
            ) from exc

    try:
        layout = Layout.model_validate(raw)
    except ValidationError as exc:
        raise LayoutMigrationError(
            f"layout failed top-level validation after widget migrations: {exc}"
        ) from exc

    return layout, migrated
