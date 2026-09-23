"""Custom OpenTelemetry metric registry for the Agora **worker**.

Issue #474 — transcode-failure telemetry, Layer 2.

This is the worker-side analogue of :mod:`cms.metrics`.  It lives in
``shared/`` (not ``cms/``) because the worker image imports only
``worker.*`` / ``shared.*`` and never ``cms.*``.  Keeping the worker's
metric handles here means call sites carry no metric-name string
literals — a typo becomes an ``AttributeError`` at import time rather
than a silently-wrong series nobody notices.

Runtime behaviour mirrors :mod:`cms.metrics`:

* When ``APPLICATIONINSIGHTS_CONNECTION_STRING`` is set and
  :func:`shared.observability.setup_observability` has run (the worker
  calls it as the first statement of ``main()``), the OTel SDK
  installed by ``azure-monitor-opentelemetry`` is the global
  ``MeterProvider`` and these counters export to App Insights as
  custom metrics.
* Otherwise OTel's default no-op ``MeterProvider`` is in effect and
  ``Counter.add(...)`` is a cheap no-op — no call site needs to gate
  on "is telemetry enabled?".

The ``opentelemetry`` API package is a transitive dependency of
``azure-monitor-opentelemetry``, which ``requirements-shared.txt`` pins
(so it lands in the worker image), so the import below is always
available in any supported worker install profile.

Naming convention
-----------------

``agora.<subsystem>.<event>`` for counters; bounded, low-cardinality
attribute keys carry the dimensions.  See the constants below for the
bounded value sets.
"""

from __future__ import annotations

from typing import Final

from opentelemetry import metrics

_meter = metrics.get_meter("agora.worker")


# ----------------------------------------------------------------------
# Transcode / job failures (worker/__main__.py dispatch)
# ----------------------------------------------------------------------
#
# This counter is the durable, queryable signal for "transcodes are
# failing" that the silent markupsafe worker-import bug (#758) lacked:
# that failure threw on every attempt but only ever surfaced in worker
# stdout.  Now each failed dispatch increments this counter with a
# bounded ``reason`` so an App Insights metric alert can fire on a
# spike of any single failure class.
#
# It is incremented once per failed dispatch attempt (so a job that
# fails and re-delivers contributes one increment per attempt) at the
# terminal-classification branches in the finalize block.  Increments
# happen ONLY for genuine failures — SIGTERM timeouts, TerminalImager
# failures, and unhandled-exception / handler-returned-False paths.
# Lease loss, mid-transcode cancellation, and success do NOT count.

transcode_failure_total: Final = _meter.create_counter(
    "agora.transcode.failure",
    description=(
        "Worker job-dispatch attempts that ended in failure, attributed "
        "by ``reason`` (import_error, timeout, imager_terminal, "
        "render_error, unknown) and ``job_type`` (the JobType value). "
        "Incremented once per failed attempt; retries of the same job "
        "each contribute one increment."
    ),
)


# Attribute keys.

ATTR_REASON: Final[str] = "reason"
ATTR_JOB_TYPE: Final[str] = "job_type"


# Bounded value set for the ``reason`` attribute.  Keep this list short
# — every distinct value is a separate series in App Insights.

# A dependency / import failed inside a handler — e.g. a missing module
# like the markupsafe bug.  Classified from ImportError /
# ModuleNotFoundError (the latter is a subclass of the former).
REASON_IMPORT_ERROR: Final[str] = "import_error"

# The worker received SIGTERM mid-job and exceeded its time budget
# (the 2-hour replica timeout path).
REASON_TIMEOUT: Final[str] = "timeout"

# A handler raised TerminalImagerError — a deterministic imager failure
# that has already flipped the BaseImage / ProvisionedImage row FAILED.
REASON_IMAGER_TERMINAL: Final[str] = "imager_terminal"

# Any other unhandled exception during processing (ffmpeg/render error,
# unexpected runtime error, etc.) that isn't an import failure.
REASON_RENDER_ERROR: Final[str] = "render_error"

# The handler returned False (failure) without raising, so there is no
# exception to classify.
REASON_UNKNOWN: Final[str] = "unknown"


# ----------------------------------------------------------------------
# Variant/job status reconciliation (cms/services/transcoder.py)
# ----------------------------------------------------------------------
#
# A variant's status and its job's status are written by different
# processes at different times, so they can drift apart.  The reaper's
# reconciliation sweep repairs the drift; this counter is how we find out
# it is happening at all, since the repair is otherwise silent.
#
# A steady non-zero rate means a write path is leaving variants stranded
# and should be fixed at source -- the sweep is a backstop, not the
# design.  ``reason=done_mismatch`` in particular should never be
# non-zero: it means a job completed successfully while its variant never
# reached READY, which the sweep deliberately refuses to repair.

variant_reconcile_total: Final = _meter.create_counter(
    "agora.variant.reconcile",
    description=(
        "Variants whose status was found inconsistent with their owning "
        "job's terminal status, attributed by ``reason`` (cancelled, "
        "poison_failed, done_mismatch). The first two are repaired; "
        "done_mismatch is reported only and indicates corruption."
    ),
)


# Bounded value set for the reconcile ``reason`` attribute.

# Newest job for the variant is CANCELLED but the variant never left
# PENDING/PROCESSING -- the pre-transcode cancel path's old behaviour.
REASON_RECONCILE_CANCELLED: Final[str] = "cancelled"

# Newest job exhausted MAX_JOB_RETRIES and is FAILED, but the variant was
# never mirrored.
REASON_RECONCILE_POISON_FAILED: Final[str] = "poison_failed"

# Newest job is DONE yet the variant is not READY.  NOT repaired: READY
# asserts a valid blob plus complete metadata, and a successful dispatch
# alone is not evidence of either.  Emitted so it can be alerted on.
REASON_RECONCILE_DONE_MISMATCH: Final[str] = "done_mismatch"
