"""Built-in widget plugins for the Composed Slide subsystem.

Importing this package registers every shipped widget into the
global :class:`cms.composed.registry.WidgetRegistry`.  Add a new
widget by creating ``cms/composed/widgets/<slug>.py`` exporting a
``Widget`` subclass, then importing + registering it below.

Phase 1A ships only the trivial text widget; future phases add
image, clock, ticker, and the minimal video widget.

Auto-registration on import means any code that does
``import cms.composed.widgets`` (or transitively touches the
bundle builder, which imports this package) gets a populated
registry.  Validator unit tests that need an isolated registry
construct a fresh :class:`WidgetRegistry` instead of using the
global one.
"""

from __future__ import annotations

from cms.composed.registry import get_registry
from cms.composed.widgets.analog_clock import AnalogClockWidget
from cms.composed.widgets.clock import ClockWidget
from cms.composed.widgets.image import ImageWidget
from cms.composed.widgets.media import MediaWidget
from cms.composed.widgets.text import TextWidget
from cms.composed.widgets.ticker import TickerWidget
from cms.composed.widgets.weather import WeatherWidget

_reg = get_registry()

# Idempotent — guards against re-import edge cases (importlib.reload,
# test session re-import) double-registering and tripping the
# registry's "already registered" guard.
for _widget_cls in (
    TextWidget,
    ImageWidget,
    MediaWidget,
    ClockWidget,
    AnalogClockWidget,
    TickerWidget,
    WeatherWidget,
):
    if not _reg.has(_widget_cls.slug):
        _reg.register(_widget_cls())

__all__ = [
    "AnalogClockWidget",
    "ClockWidget",
    "ImageWidget",
    "MediaWidget",
    "TextWidget",
    "TickerWidget",
    "WeatherWidget",
]
