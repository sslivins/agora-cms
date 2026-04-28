"""Tests for the central metrics registry (``cms.metrics``).

The registry is intentionally lightweight — its job is to provide
process-wide OpenTelemetry counter handles that the rest of the CMS can
import and call ``.add()`` on without each call site having to know
whether telemetry is wired up.

These tests verify the *behavioural* contract:

* importing the module never raises (even with no SDK configured),
* every documented handle is present and exposes ``.add()``, and
* calling ``.add()`` is safe under the default no-op MeterProvider that
  applies when ``configure_azure_monitor`` has not run.

We deliberately avoid asserting anything about the *type* of the
returned counter objects — OTel changes those classes between minor
versions, and pinning to a concrete class would cause spurious
breakage on every dependency bump.
"""

from __future__ import annotations

import pytest


def test_module_imports_without_telemetry_configured():
    # Smoke test.  ``cms.metrics`` is imported at module-load by every
    # subsystem that emits a counter; if it ever throws on import we
    # break all of them at once.
    import importlib

    import cms.metrics as m

    importlib.reload(m)


def test_wps_counter_handles_exposed():
    from cms import metrics

    for name in (
        "wps_send_attempt_total",
        "wps_send_success_total",
        "wps_send_failed_total",
    ):
        handle = getattr(metrics, name)
        assert hasattr(handle, "add"), (
            f"{name} should expose an OTel-style .add() method"
        )


def test_wps_failure_reason_constants_are_strings():
    from cms import metrics

    # Bounded value set for the ``reason`` attribute — every distinct
    # value is a series in App Insights, so this list deliberately
    # short.  Pin it so a future PR adding a new reason has to update
    # this test (and the workbook KQL alongside).
    assert metrics.ATTR_REASON == "reason"
    assert {
        metrics.WPS_REASON_404,
        metrics.WPS_REASON_429,
        metrics.WPS_REASON_HTTP_ERROR,
        metrics.WPS_REASON_UNEXPECTED,
    } == {"404", "429", "http_error", "unexpected"}


@pytest.mark.parametrize(
    "name",
    ["wps_send_attempt_total", "wps_send_success_total"],
)
def test_unattributed_add_is_safe(name):
    from cms import metrics

    handle = getattr(metrics, name)
    # Under the default no-op MeterProvider this is a no-op; under a
    # real SDK it records.  Either way it must not raise.
    handle.add(1)


def test_failure_add_with_reason_attribute_is_safe():
    from cms import metrics

    metrics.wps_send_failed_total.add(
        1, {metrics.ATTR_REASON: metrics.WPS_REASON_404},
    )
    metrics.wps_send_failed_total.add(
        1, {metrics.ATTR_REASON: metrics.WPS_REASON_429},
    )
    metrics.wps_send_failed_total.add(
        1, {metrics.ATTR_REASON: metrics.WPS_REASON_HTTP_ERROR},
    )
    metrics.wps_send_failed_total.add(
        1, {metrics.ATTR_REASON: metrics.WPS_REASON_UNEXPECTED},
    )
