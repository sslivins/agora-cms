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
  window-resize fallback).  Returns a zero-arg refit handle so callers
  whose content changes over time (e.g. a date that rolls over) can
  re-fit on demand.

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
  return refit;
};
""".strip()
