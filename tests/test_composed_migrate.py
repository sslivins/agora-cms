"""Tests for :mod:`cms.composed.migrate` — load-time config migration.

The point of these tests is to prove the migration mechanism actually
works without forcing any production widget to bump its
``config_version``.  We register a transient test widget that has a
``config_version=3`` and a hand-rolled ``migrate_config`` that walks
v1→v2→v3 step-by-step, then exercise every interesting failure mode
through the public :func:`cms.composed.migrate.load_and_migrate` API.
"""

from __future__ import annotations

import uuid
from typing import ClassVar

import pytest
from pydantic import BaseModel, ConfigDict, Field

import cms.composed.widgets  # noqa: F401 — trigger registration
from cms.composed.migrate import (
    LayoutMigrationError,
    UnknownWidgetTypeError,
    UnsupportedSchemaVersionError,
    WidgetConfigForwardVersionError,
    load_and_migrate,
)
from cms.composed.registry import Widget, WidgetRender, get_registry
from cms.composed.schema import Cell, SCHEMA_VERSION


# ── Test widget that drives v1→v3 migration ──────────────────────────


class _MigrationTestConfigV3(BaseModel):
    """The v3 (current) shape: renamed + restructured fields."""

    model_config = ConfigDict(extra="forbid")

    text_color: str = Field(default="#ffffff")
    background: dict = Field(default_factory=lambda: {"color": "#000000"})
    label: str = Field(default="hello")


class _MigrationTestWidget(Widget):
    """Test-only widget with a non-trivial multi-step migration.

    v1 had ``color`` and ``bg_color`` at the top level.
    v2 renamed ``color`` to ``text_color`` (bg_color unchanged).
    v3 restructured ``bg_color`` into a nested ``background.color`` dict
    and added a required-with-default ``label``.

    Each step is intentionally non-trivial so the test covers both
    renames and shape changes — additive-only migrations aren't
    interesting because they don't need ``migrate_config`` at all.
    """

    slug: ClassVar[str] = "_migration_test"
    display_name: ClassVar[str] = "Migration Test"
    icon: ClassVar[str] = "🧪"
    ConfigSchema: ClassVar[type[BaseModel]] = _MigrationTestConfigV3
    config_version: ClassVar[int] = 3

    def default_config(self) -> dict:
        return _MigrationTestConfigV3().model_dump()

    def render_html(self, config, cell, instance_id, ctx=None) -> WidgetRender:
        return WidgetRender(html=f'<div id="{instance_id}"></div>', css="", js="")

    def editor_template(self) -> str:
        return "widgets/migration_test.html.j2"

    def migrate_config(self, raw: dict, from_version: int) -> dict:
        upgraded = dict(raw)
        if from_version == 1:
            # v1 -> v2: rename ``color`` -> ``text_color``
            if "color" in upgraded:
                upgraded["text_color"] = upgraded.pop("color")
        elif from_version == 2:
            # v2 -> v3: nest ``bg_color`` under ``background.color`` +
            # add ``label`` default.
            bg_color = upgraded.pop("bg_color", "#000000")
            upgraded["background"] = {"color": bg_color}
            upgraded.setdefault("label", "hello")
        else:
            raise ValueError(
                f"unexpected from_version {from_version} for migration test widget"
            )
        return upgraded


@pytest.fixture
def _registered_migration_widget():
    """Register the test widget for the duration of one test."""
    registry = get_registry()
    widget = _MigrationTestWidget()
    registry.register(widget)
    try:
        yield widget
    finally:
        # Best-effort cleanup so other tests don't see us.
        registry._widgets.pop("_migration_test", None)


def _layout_with(widget_entries: list[dict]) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "widgets": widget_entries,
    }


def _widget_entry(
    slug: str,
    config: dict,
    config_version: int,
    *,
    cell: dict | None = None,
) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "type": slug,
        "cell": cell or {"row": 1, "col": 1, "rowspan": 1, "colspan": 1},
        "config": config,
        "config_version": config_version,
    }


# ── Happy path: no-op and stepwise migrations ────────────────────────


class TestNoOpAndStepwiseMigration:
    def test_no_widgets_returns_unmigrated(self):
        layout, migrated = load_and_migrate(_layout_with([]))
        assert migrated is False
        assert layout.widgets == []

    def test_widget_at_current_version_is_no_op(self, _registered_migration_widget):
        entry = _widget_entry(
            "_migration_test",
            _MigrationTestConfigV3().model_dump(),
            config_version=3,
        )
        layout, migrated = load_and_migrate(_layout_with([entry]))
        assert migrated is False
        assert layout.widgets[0].config_version == 3

    def test_v1_walks_through_to_v3(self, _registered_migration_widget):
        # v1-shaped config: ``color`` and ``bg_color`` at top level.
        v1_config = {"color": "#ff0000", "bg_color": "#00ff00"}
        entry = _widget_entry("_migration_test", v1_config, config_version=1)
        layout, migrated = load_and_migrate(_layout_with([entry]))
        assert migrated is True
        assert layout.widgets[0].config_version == 3
        cfg = layout.widgets[0].config
        # v1 -> v2 rename succeeded
        assert cfg["text_color"] == "#ff0000"
        # v2 -> v3 restructure succeeded
        assert cfg["background"] == {"color": "#00ff00"}
        # v3 added field has its default
        assert cfg["label"] == "hello"
        # Old keys no longer present (extra='forbid' would have rejected them anyway)
        assert "color" not in cfg
        assert "bg_color" not in cfg

    def test_v2_walks_only_one_step(self, _registered_migration_widget):
        v2_config = {"text_color": "#abcdef", "bg_color": "#fedcba"}
        entry = _widget_entry("_migration_test", v2_config, config_version=2)
        layout, migrated = load_and_migrate(_layout_with([entry]))
        assert migrated is True
        assert layout.widgets[0].config_version == 3
        cfg = layout.widgets[0].config
        assert cfg["text_color"] == "#abcdef"
        assert cfg["background"] == {"color": "#fedcba"}

    def test_mixed_versions_in_same_layout(self, _registered_migration_widget):
        v1 = _widget_entry(
            "_migration_test",
            {"color": "#111111", "bg_color": "#222222"},
            config_version=1,
            cell={"row": 1, "col": 1, "rowspan": 1, "colspan": 1},
        )
        v3 = _widget_entry(
            "_migration_test",
            _MigrationTestConfigV3().model_dump(),
            config_version=3,
            cell={"row": 2, "col": 1, "rowspan": 1, "colspan": 1},
        )
        layout, migrated = load_and_migrate(_layout_with([v1, v3]))
        assert migrated is True
        assert layout.widgets[0].config_version == 3
        assert layout.widgets[1].config_version == 3
        assert layout.widgets[0].config["text_color"] == "#111111"

    def test_existing_text_widget_stays_at_v1(self):
        """Real production widgets are still v1; loading them is a no-op."""
        entry = _widget_entry(
            "text",
            {
                "text": "hi",
                "color": "#ffffff",
                "font_size_px": 24,
                "font_family": "sans",
            },
            config_version=1,
        )
        layout, migrated = load_and_migrate(_layout_with([entry]))
        assert migrated is False
        assert layout.widgets[0].config_version == 1


# ── Failure modes ────────────────────────────────────────────────────


class TestMigrationFailureModes:
    def test_unknown_widget_type_raises(self):
        entry = _widget_entry("_does_not_exist", {}, config_version=1)
        with pytest.raises(UnknownWidgetTypeError, match="_does_not_exist"):
            load_and_migrate(_layout_with([entry]))

    def test_forward_version_raises(self, _registered_migration_widget):
        # Widget is at v3; instance claims v4 → CMS is older than data.
        entry = _widget_entry("_migration_test", {}, config_version=4)
        with pytest.raises(WidgetConfigForwardVersionError, match="older than"):
            load_and_migrate(_layout_with([entry]))

    def test_unsupported_layout_schema_version_raises(self):
        raw = {"schema_version": 999, "widgets": []}
        with pytest.raises(UnsupportedSchemaVersionError, match="999"):
            load_and_migrate(raw)

    def test_non_dict_payload_raises(self):
        with pytest.raises(LayoutMigrationError, match="must be a dict"):
            load_and_migrate("not a dict")  # type: ignore[arg-type]

    def test_widgets_not_a_list_raises(self):
        with pytest.raises(LayoutMigrationError, match="widgets must be a list"):
            load_and_migrate({"schema_version": SCHEMA_VERSION, "widgets": {}})

    def test_widget_entry_not_a_dict_raises(self):
        with pytest.raises(LayoutMigrationError, match="must be a dict"):
            load_and_migrate(
                {"schema_version": SCHEMA_VERSION, "widgets": ["nope"]}
            )

    def test_widget_entry_missing_type_raises(self):
        bad = {
            "id": str(uuid.uuid4()),
            "cell": {"row": 1, "col": 1, "rowspan": 1, "colspan": 1},
            "config": {},
            "config_version": 1,
        }
        with pytest.raises(LayoutMigrationError, match="missing required 'type'"):
            load_and_migrate(_layout_with([bad]))

    def test_migration_producing_invalid_config_raises(
        self, _registered_migration_widget
    ):
        # Pass v1 config with a junk key that the migration won't strip.
        # After v1->v2 rename, "junk" survives and v3 schema (extra=forbid)
        # rejects it.
        v1_with_junk = {"color": "#fff", "bg_color": "#000", "junk": "nope"}
        entry = _widget_entry("_migration_test", v1_with_junk, config_version=1)
        with pytest.raises(LayoutMigrationError, match="failed validation"):
            load_and_migrate(_layout_with([entry]))

    def test_widget_migrate_config_returns_non_dict_raises(
        self, _registered_migration_widget, monkeypatch
    ):
        widget = get_registry().get("_migration_test")
        monkeypatch.setattr(
            widget, "migrate_config", lambda raw, from_version: "not a dict"
        )
        entry = _widget_entry("_migration_test", {}, config_version=1)
        with pytest.raises(LayoutMigrationError, match="returned non-dict"):
            load_and_migrate(_layout_with([entry]))


# ── Persist-only-once semantics ──────────────────────────────────────


class TestPersistOnce:
    def test_migrated_flag_is_idempotent_after_resave(
        self, _registered_migration_widget
    ):
        v1_config = {"color": "#abc", "bg_color": "#def"}
        entry = _widget_entry("_migration_test", v1_config, config_version=1)
        layout1, migrated1 = load_and_migrate(_layout_with([entry]))
        assert migrated1 is True

        # Simulate the caller persisting via model_dump and reloading.
        resaved = {
            "schema_version": SCHEMA_VERSION,
            "widgets": [layout1.widgets[0].model_dump(mode="json")],
        }
        layout2, migrated2 = load_and_migrate(resaved)
        assert migrated2 is False
        assert layout2.widgets[0].config == layout1.widgets[0].config
        assert layout2.widgets[0].config_version == 3
