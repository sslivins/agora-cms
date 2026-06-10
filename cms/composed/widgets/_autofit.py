"""Shared client-side font auto-fit ("shrink to fit") helper.

Exposes :data:`AUTOFIT_JS` — a self-contained, idempotent JS snippet that
defines two globals used by text-bearing widgets whose config has
``shrink_to_fit`` enabled:

* ``window.__cwFit(inner, maxPx, minPx)`` — binary-searches the largest
  font size (px) at which ``inner`` fits within its parent box
  (``inner.parentElement``) in both width and height, and applies it as
  an inline ``font-size``.
* ``window.__cwFitObserve(inner, maxPx, minPx)`` — runs ``__cwFit`` once
  and re-runs it whenever the box resizes (via ``ResizeObserver``, with a
  window-resize fallback) **or whenever the measured element's content
  changes** (via ``MutationObserver`` watching ``childList`` +
  ``characterData``, but NOT ``attributes`` — so the fit's own inline
  ``font-size`` write never retriggers it).  Returns a zero-arg refit
  handle so callers can also re-fit on demand.

  The ``MutationObserver`` is what lets *dynamic* widgets (a ticking
  clock, a counting-down timer, a rotating RSS list, a weather refresh)
  re-fit automatically: they just paint into child nodes as usual and
  the observer refits — **no per-widget refit wiring required**.  A
  static widget (plain text) simply never mutates and pays nothing.

Both globals are guarded (``window.X = window.X || function(){...}``) so
embedding ``AUTOFIT_JS`` once per bundle is sufficient even when many
widget instances use it; the bundle builder also de-dupes ``js`` blocks
by content hash, so this constant embeds exactly once.

The measured element's ``parentElement`` MUST be the bounded box: a
flex-centered box with an inner content node satisfies this (text wraps
at the box width, so overflow is detected via ``scrollWidth`` /
``scrollHeight`` exceeding the box's client size).
"""

from __future__ import annotations

# Bounds match the widget font-size schema (ge=8, le=512).  Kept here so
# widgets and tests reference a single source of truth.
AUTOFIT_MAX_PX = 512
AUTOFIT_MIN_PX = 8

AUTOFIT_JS = r"""
window.__cwFit = window.__cwFit || function (inner, maxPx, minPx) {
  if (!inner) return;
  var box = inner.parentElement;
  if (!box) return;
  var hi = (typeof maxPx === 'number' && maxPx > 0) ? maxPx : 512;
  var lo = (typeof minPx === 'number' && minPx > 0) ? minPx : 8;
  if (hi < lo) { var t = hi; hi = lo; lo = t; }
  if (!box.clientWidth || !box.clientHeight) return;
  var best = lo;
  // ~14 iterations converges to <0.1px across the 8..512px range.
  for (var i = 0; i < 14; i++) {
    var mid = (lo + hi) / 2;
    inner.style.fontSize = mid + 'px';
    if (inner.scrollWidth <= box.clientWidth &&
        inner.scrollHeight <= box.clientHeight) {
      best = mid; lo = mid;
    } else {
      hi = mid;
    }
  }
  inner.style.fontSize = best + 'px';
};
window.__cwFitObserve = window.__cwFitObserve || function (inner, maxPx, minPx) {
  if (!inner) return function () {};
  var refit = function () { window.__cwFit(inner, maxPx, minPx); };
  refit();
  var box = inner.parentElement;
  if (box && typeof ResizeObserver !== 'undefined') {
    try {
      new ResizeObserver(function () { refit(); }).observe(box);
    } catch (e) {
      window.addEventListener('resize', refit);
    }
  } else {
    window.addEventListener('resize', refit);
  }
  // Re-fit when the measured content changes (clock tick, countdown,
  // RSS rotation, weather refresh, ...).  childList + characterData
  // only: observing attributes would loop because __cwFit writes
  // inner.style.fontSize on every fit.
  if (typeof MutationObserver !== 'undefined') {
    try {
      new MutationObserver(function () { refit(); }).observe(inner, {
        childList: true, characterData: true, subtree: true
      });
    } catch (e) { /* no-op: resize observer still covers box changes */ }
  }
  return refit;
};
""".strip()


def autofit_inner_init_js(inner_id: str) -> str:
    """Return the standard one-line per-instance autofit init.

    Every "shrink to fit" widget wraps its content in an inner element
    (``inner_id``) and fits that element against its bounded parent box.
    This helper emits the exact init statement that text.py's shrink path
    has used since the feature shipped, so each widget concatenates it
    after its own (content-painting) ``init_js`` instead of re-deriving
    the ``__cwFitObserve`` call by hand.

    ``inner_id`` is always a CMS-generated DOM id (``cw-<slug>-inner-<uuid>``)
    so it is safe to embed as a single-quoted JS string literal.
    """
    return (
        f"var el=document.getElementById('{inner_id}');"
        f"if(el&&window.__cwFitObserve)"
        f"window.__cwFitObserve(el,{AUTOFIT_MAX_PX},{AUTOFIT_MIN_PX});"
    )
