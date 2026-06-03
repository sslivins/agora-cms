"""Composed Slide subsystem.

Modules:

* :mod:`cms.composed.schema` — Pydantic layout / cell / widget-instance
  schemas. Locked-in v1 constraints: fixed 1920×1080 canvas, fixed
  12×8 grid.
* :mod:`cms.composed.registry` — widget plugin contract + global
  registry. Every widget subclasses :class:`Widget`.
* :mod:`cms.composed.validate` — semantic layout validator. Runs in
  addition to Pydantic shape validation on save, on AI output, and
  defensively at bundle build time.

Phase 1A adds ``cms.composed.bundle`` (the self-contained HTML
builder).  Phase 1B+ adds widgets under ``cms.composed.widgets.``.
"""
