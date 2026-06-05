"""Semantic layout validator.

Pydantic only validates the *shape* of a layout document.  This
module adds the cross-cutting and per-widget checks the editor, the
AI generate endpoint, and (defensively) the bundle builder all need:

* Cells fit within grid bounds (also enforced by Pydantic for single
  cells, but rowspan/colspan can still push past the edge).
* Every widget instance's ``type`` is in the registry.
* Per-widget config validates against ``ConfigSchema`` and the
  widget's own ``validate_semantic`` hook.
* Widget instance IDs are unique within the layout.

The validator returns a list of :class:`ValidationError` rather than
raising, so the editor can surface multiple problems at once.
Empty list = ok.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import ValidationError as PydanticValidationError

from cms.composed.registry import Widget, WidgetRegistry, get_registry
from cms.composed.schema import GRID_COLS, GRID_ROWS, Cell, Layout, WidgetInstance


@dataclass
class ValidationError:
    """One human-readable layout problem.

    ``widget_id`` is set when the problem is scoped to a specific
    widget instance; ``None`` for layout-wide issues.
    """

    code: str
    message: str
    widget_id: str | None = None


def _cell_out_of_bounds(cell: Cell) -> bool:
    return (
        cell.row + cell.rowspan - 1 > GRID_ROWS
        or cell.col + cell.colspan - 1 > GRID_COLS
    )


def _validate_one_widget(
    instance: WidgetInstance,
    widget: Widget,
) -> list[ValidationError]:
    """Run Pydantic + semantic validation on one widget's config."""
    errors: list[ValidationError] = []

    try:
        config_model = widget.ConfigSchema.model_validate(instance.config)
    except PydanticValidationError as exc:
        for err in exc.errors():
            loc = ".".join(str(p) for p in err.get("loc", ()))
            errors.append(
                ValidationError(
                    code="widget_config_shape",
                    message=f"{loc or 'config'}: {err.get('msg', 'invalid')}",
                    widget_id=str(instance.id),
                )
            )
        # If shape is wrong, semantic validation will likely also fail
        # in confusing ways — stop here for this widget.
        return errors

    for msg in widget.validate_semantic(config_model):
        errors.append(
            ValidationError(
                code="widget_config_semantic",
                message=msg,
                widget_id=str(instance.id),
            )
        )

    return errors


def validate_layout(
    layout: Layout,
    registry: WidgetRegistry | None = None,
) -> list[ValidationError]:
    """Validate a layout against the registry; return all problems."""
    reg = registry if registry is not None else get_registry()
    errors: list[ValidationError] = []

    # ── Widget instance ID uniqueness ───────────────────────────
    seen_ids: set[str] = set()
    for inst in layout.widgets:
        wid = str(inst.id)
        if wid in seen_ids:
            errors.append(
                ValidationError(
                    code="duplicate_widget_id",
                    message=f"duplicate widget instance id {wid}",
                    widget_id=wid,
                )
            )
        seen_ids.add(wid)

    # ── Per-widget bounds ───────────────────────────────────────
    # Widgets MAY overlap: stacking order is the ``layout.widgets``
    # array order (later = painted on top), mirrored by an explicit
    # ``z-index`` in the bundle.  So the only cell-geometry rule left
    # is that a widget must fit inside the grid.
    for inst in layout.widgets:
        wid = str(inst.id)

        if _cell_out_of_bounds(inst.cell):
            errors.append(
                ValidationError(
                    code="cell_out_of_bounds",
                    message=(
                        f"widget {wid} occupies "
                        f"({inst.cell.row},{inst.cell.col}) "
                        f"+{inst.cell.rowspan}x{inst.cell.colspan} "
                        f"which extends past the {GRID_ROWS}x{GRID_COLS} grid"
                    ),
                    widget_id=wid,
                )
            )

    # ── Per-widget config validation via registry ───────────────
    for inst in layout.widgets:
        wid = str(inst.id)
        widget = reg.get(inst.type)
        if widget is None:
            errors.append(
                ValidationError(
                    code="unknown_widget_type",
                    message=f"widget {wid}: unknown widget type {inst.type!r}",
                    widget_id=wid,
                )
            )
            continue

        errors.extend(_validate_one_widget(inst, widget))

    return errors
