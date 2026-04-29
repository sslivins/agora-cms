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


# ----------------------------------------------------------------------
# Scheduler (cms/services/scheduler.py)
# ----------------------------------------------------------------------
#
# Two counters cover the operational questions operators actually ask
# about the scheduler:
#
# 1. "Is the scheduler running?"  ``agora.scheduler.tick`` with
#    ``outcome`` ∈ {evaluated, skipped_not_leader, error} answers this.
#    Under N replicas, total tick rate scales with replica count, but
#    ``outcome=evaluated`` count ≈ global tick rate (one leader at a
#    time).  ``outcome=skipped_not_leader`` is a positive liveness
#    signal for follower replicas and verifies the leader lease is
#    gating correctly.
# 2. "Are devices missing scheduled playback?"
#    ``agora.scheduler.missed_emitted`` increments once per MISSED
#    ScheduleLog row that is durably committed.  We deliberately do
#    NOT increment for CAS-claim losses (normal under multi-replica
#    dedup) or for log-write failures (the CAS claim is reverted so
#    the next tick retries).  The increment happens AFTER the outer
#    ``db.commit()`` succeeds so a commit failure cannot produce
#    false-positive telemetry.

scheduler_tick_total: Final = _meter.create_counter(
    "agora.scheduler.tick",
    description=(
        "Scheduler loop iterations, attributed by ``outcome``: "
        "``evaluated`` (leader ran ``evaluate_schedules`` to "
        "completion), ``skipped_not_leader`` (replica without the "
        "scheduler lease), or ``error`` (unexpected exception in the "
        "loop body — does NOT include task cancellation)."
    ),
)

scheduler_missed_emitted_total: Final = _meter.create_counter(
    "agora.scheduler.missed_emitted",
    description=(
        "MISSED schedule events that were durably committed to "
        "``schedule_logs`` in the current tick.  Increments only "
        "after ``db.commit()`` succeeds and only for events whose "
        "ScheduleLog row was actually written (CAS-claim losses and "
        "log-write failures are not counted)."
    ),
)


# Bounded value set for the ``outcome`` attribute on
# ``agora.scheduler.tick``.  Keep this list short — every distinct
# value is a separate series in App Insights.

ATTR_OUTCOME: Final[str] = "outcome"

SCHEDULER_OUTCOME_EVALUATED: Final[str] = "evaluated"
SCHEDULER_OUTCOME_SKIPPED_NOT_LEADER: Final[str] = "skipped_not_leader"
SCHEDULER_OUTCOME_ERROR: Final[str] = "error"


# ----------------------------------------------------------------------
# Presence / leader-lease (cms/services/leader.py)
# ----------------------------------------------------------------------
#
# The ``LeaderLease`` heartbeat task drives a small state machine for
# each loop (scheduler, alert_sweep, …).  Three counters expose the
# state-change events we care about during incidents:
#
# * ``agora.presence.claim`` — this replica's lease state flipped to
#   leader.  Sums to roughly N over the cluster's lifetime where N is
#   the number of leadership transitions; under steady state with no
#   restarts you should see one increment per replica per
#   ``(loop_name)`` cluster-wide.  A sustained spike means leadership
#   is flapping.
# * ``agora.presence.claim_lost`` — this replica observed that it lost
#   the lease (heartbeat returned False after previously being True).
#   Graceful shutdown (``stop()``) is intentionally NOT counted — only
#   involuntary loss (lease takeover, heartbeat exception, etc.).  The
#   delta between ``claim`` and ``claim_lost`` cluster-wide should
#   stay near zero in steady state.
# * ``agora.presence.heartbeat_late`` — the heartbeat raised an
#   exception (DB unreachable, transient network issue, …).  Treated
#   as a missed renewal in the lease state machine; the loop will
#   recover on the next heartbeat.  Frequent ticks here are an early
#   warning that the lease is at risk of TTL'ing out.
#
# Dimension: ``loop_name`` (bounded enum — currently "scheduler"; will
# expand as alert_sweep / log_drainer / rollout adopt the lease).  We
# deliberately do NOT use ``replica_id`` / ``holder_id`` as a
# dimension — the holder UUID rotates every process restart and would
# produce unbounded series in App Insights.
#
# Non-Postgres backends (SQLite tests, local dev with no DB) bypass
# the heartbeat entirely and report ``is_leader=True`` unconditionally.
# In that mode no presence counters are emitted — there is no real
# state machine to instrument and any signal would be noise.

presence_claim_total: Final = _meter.create_counter(
    "agora.presence.claim",
    description=(
        "Leader lease state flipped to leader on this replica "
        "(state True after previously being False, or won on first "
        "acquire).  Dimension: ``loop_name``."
    ),
)

presence_claim_lost_total: Final = _meter.create_counter(
    "agora.presence.claim_lost",
    description=(
        "Leader lease state flipped to non-leader on this replica "
        "(heartbeat returned False after previously being True). "
        "Graceful shutdown via ``stop()`` is not counted.  "
        "Dimension: ``loop_name``."
    ),
)

presence_heartbeat_late_total: Final = _meter.create_counter(
    "agora.presence.heartbeat_late",
    description=(
        "Leader lease heartbeat raised an exception (treated as a "
        "missed renewal).  Dimension: ``loop_name``."
    ),
)


# Attribute key for the lease-loop name on presence counters.

ATTR_LOOP_NAME: Final[str] = "loop_name"
