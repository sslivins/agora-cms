"""Custom OpenTelemetry metric registry for the Agora CMS.

Issue #474, Phase 0 / Pillar B (custom metrics) — first slice.

Defining all counter/histogram/gauge handles in **one place** has two
benefits:

* No metric-name string literals at call sites — typos at the call
  site become ``AttributeError`` at import time, not silently-wrong
  metric names that nobody notices for weeks.
* The registry doubles as a catalog: ``grep`` ``cms/metrics.py`` and
  you can see every custom metric the CMS emits.

Runtime behaviour:

* When ``APPLICATIONINSIGHTS_CONNECTION_STRING`` is set and
  :func:`cms.observability.setup_observability` has run, the OTel SDK
  installed by ``azure-monitor-opentelemetry`` becomes the global
  ``MeterProvider`` and these counters export to App Insights as
  custom metrics.
* When telemetry is disabled (local dev, unit tests, ``docker-compose``,
  CI without the env var), OTel's default no-op
  ``MeterProvider`` is in effect.  ``Counter.add(...)`` then resolves
  to a cheap no-op call — there is no need to gate any call site on
  "is telemetry enabled?".

The ``opentelemetry`` API package is a transitive dependency of
``azure-monitor-opentelemetry`` (a hard dep in ``requirements.txt``),
so the imports below are always available in any supported install
profile.

Naming convention
-----------------

Per the telemetry plan we use ``agora.<subsystem>.<event>`` for
counters.  Bounded, low-cardinality attribute keys carry the dimensions
(e.g. ``reason`` for WPS send failures).  See the constants below for
the bounded value sets.
"""

from __future__ import annotations

from typing import Final

from opentelemetry import metrics

_meter = metrics.get_meter("agora.cms")


# ----------------------------------------------------------------------
# WPS transport (cms/services/wps_transport.py)
# ----------------------------------------------------------------------
#
# Failure-rate model:
#   failure_rate = wps.send.failure / wps.send.attempt
#   success_rate = wps.send.success / wps.send.attempt
# The attempt counter is incremented unconditionally before the send
# so the denominator covers every code path including unexpected
# exceptions.  ``success`` and ``failure`` are mutually exclusive and
# together always sum to ``attempt``.

wps_send_attempt_total: Final = _meter.create_counter(
    "agora.wps.send.attempt",
    description=(
        "Total number of WPS send_to_device calls attempted, including "
        "those that ultimately failed.  Forms the denominator of the "
        "WPS send failure rate."
    ),
)

wps_send_success_total: Final = _meter.create_counter(
    "agora.wps.send.success",
    description=(
        "WPS send_to_device calls that returned successfully (HTTP 2xx)."
    ),
)

wps_send_failed_total: Final = _meter.create_counter(
    "agora.wps.send.failure",
    description=(
        "WPS send_to_device calls that failed for any reason.  The "
        "``reason`` attribute distinguishes 404 (device offline), 429 "
        "(throttled by WPS), other HTTP errors, and unexpected "
        "exceptions."
    ),
)


# Bounded value set for the ``reason`` attribute on WPS failure
# counters.  Keep this list short — every distinct value is a separate
# series in App Insights.

ATTR_REASON: Final[str] = "reason"

WPS_REASON_404: Final[str] = "404"
WPS_REASON_429: Final[str] = "429"
WPS_REASON_HTTP_ERROR: Final[str] = "http_error"
WPS_REASON_UNEXPECTED: Final[str] = "unexpected"
