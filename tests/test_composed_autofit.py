"""Tests for the shared shrink-to-fit helper cms.composed.widgets._autofit.

These pin the cross-widget contract: a single binary-search fitter, a
ResizeObserver + MutationObserver wiring (so dynamic widgets refit on
content change without per-widget code), and a compact one-line
per-instance init emitter reused verbatim by every shrink-to-fit widget.
"""

from __future__ import annotations

from cms.composed.widgets._autofit import (
    AUTOFIT_JS,
    AUTOFIT_MAX_PX,
    AUTOFIT_MIN_PX,
    autofit_inner_init_js,
)


class TestAutofitConstants:
    def test_bounds_match_widget_schema(self):
        # Widgets declare font_size_px Field(ge=8, le=512); the fitter's
        # bounds must be the same single source of truth.
        assert AUTOFIT_MAX_PX == 512
        assert AUTOFIT_MIN_PX == 8


class TestAutofitJs:
    def test_defines_both_globals(self):
        assert "window.__cwFit" in AUTOFIT_JS
        assert "window.__cwFitObserve" in AUTOFIT_JS

    def test_globals_are_idempotent_guarded(self):
        # Embedding the snippet once per bundle must be safe even with
        # many instances: the globals self-guard.
        assert "window.__cwFit = window.__cwFit ||" in AUTOFIT_JS
        assert "window.__cwFitObserve = window.__cwFitObserve ||" in AUTOFIT_JS

    def test_observes_resize_and_mutations(self):
        assert "ResizeObserver" in AUTOFIT_JS
        assert "MutationObserver" in AUTOFIT_JS

    def test_mutation_observer_excludes_attributes(self):
        # Observing attributes would loop: __cwFit writes inner.style
        # .fontSize on every fit, which would retrigger the observer.
        assert "childList: true, characterData: true, subtree: true" in AUTOFIT_JS
        assert "attributes: true" not in AUTOFIT_JS

    def test_fits_against_parent_box(self):
        assert "inner.parentElement" in AUTOFIT_JS
        assert "scrollWidth" in AUTOFIT_JS
        assert "scrollHeight" in AUTOFIT_JS


class TestAutofitInnerInitJs:
    def test_emits_observe_call_for_inner_id(self):
        out = autofit_inner_init_js("cw-text-inner-abcd")
        assert "document.getElementById('cw-text-inner-abcd')" in out
        assert "window.__cwFitObserve" in out

    def test_passes_shared_bounds(self):
        out = autofit_inner_init_js("cw-clock-inner-x")
        assert f"{AUTOFIT_MAX_PX},{AUTOFIT_MIN_PX}" in out

    def test_guards_missing_element_and_helper(self):
        out = autofit_inner_init_js("cw-rss-inner-x")
        assert "if(el&&window.__cwFitObserve)" in out

    def test_is_single_statement_line(self):
        # Reused as a concatenated suffix after each widget's own init_js,
        # so it must be a single self-contained statement (no newline).
        out = autofit_inner_init_js("cw-weather-inner-x")
        assert "\n" not in out
        assert out.endswith(";")
