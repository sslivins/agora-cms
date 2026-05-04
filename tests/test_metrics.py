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
    #
    # Run in a subprocess so we exercise the cold-import path without
    # polluting the parent test process's module cache.  Importantly,
    # we MUST NOT call ``importlib.reload(cms.metrics)`` here:
    # consumer modules (e.g. ``cms.services.leader``) bind the counter
    # handles via ``from cms.metrics import presence_claim_total``,
    # which captures object identity.  Reloading rebinds the names in
    # ``cms.metrics`` to brand-new counter objects but leaves the
    # consumer-cached references pointing at the originals.  Tests
    # that monkey-patch ``cms.metrics.<handle>.add`` then silently
    # fail to intercept calls leader.py makes through its cached
    # reference.  The bug surfaces non-deterministically depending on
    # xdist's per-file distribution; see the four-test failure cluster
    # in ``tests/test_presence_metrics.py`` exposed when other test
    # files were added.
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-c", "import cms.metrics"],
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"cold-import of cms.metrics failed:\n"
        f"stdout={result.stdout.decode()!r}\n"
        f"stderr={result.stderr.decode()!r}"
    )


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


def test_scheduler_counter_handles_exposed():
    from cms import metrics

    for name in (
        "scheduler_tick_total",
        "scheduler_missed_emitted_total",
    ):
        handle = getattr(metrics, name)
        assert hasattr(handle, "add"), (
            f"{name} should expose an OTel-style .add() method"
        )


def test_scheduler_outcome_constants_are_bounded():
    from cms import metrics

    # Bounded value set for the ``outcome`` attribute on the scheduler
    # tick counter.  Pin this so a future PR adding a new outcome has
    # to update this test (and any KQL workbook tile that filters on
    # outcome) alongside.
    assert metrics.ATTR_OUTCOME == "outcome"
    assert {
        metrics.SCHEDULER_OUTCOME_EVALUATED,
        metrics.SCHEDULER_OUTCOME_SKIPPED_NOT_LEADER,
        metrics.SCHEDULER_OUTCOME_ERROR,
    } == {"evaluated", "skipped_not_leader", "error"}


def test_scheduler_tick_add_with_outcome_is_safe():
    from cms import metrics

    for outcome in (
        metrics.SCHEDULER_OUTCOME_EVALUATED,
        metrics.SCHEDULER_OUTCOME_SKIPPED_NOT_LEADER,
        metrics.SCHEDULER_OUTCOME_ERROR,
    ):
        metrics.scheduler_tick_total.add(
            1, {metrics.ATTR_OUTCOME: outcome},
        )


def test_scheduler_missed_emitted_add_is_safe():
    from cms import metrics

    metrics.scheduler_missed_emitted_total.add(3)


def test_presence_counter_handles_exposed():
    from cms import metrics

    for name in (
        "presence_claim_total",
        "presence_claim_lost_total",
        "presence_heartbeat_late_total",
    ):
        handle = getattr(metrics, name)
        assert hasattr(handle, "add"), (
            f"{name} should expose an OTel-style .add() method"
        )


def test_presence_loop_name_attribute_constant():
    from cms import metrics

    assert metrics.ATTR_LOOP_NAME == "loop_name"


def test_presence_add_with_loop_name_is_safe():
    from cms import metrics

    for handle_name in (
        "presence_claim_total",
        "presence_claim_lost_total",
        "presence_heartbeat_late_total",
    ):
        handle = getattr(metrics, handle_name)
        handle.add(1, {metrics.ATTR_LOOP_NAME: "scheduler"})
