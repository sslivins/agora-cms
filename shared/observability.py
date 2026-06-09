"""Application Insights / OpenTelemetry bootstrap for the CMS and worker.

Issue #474, Phase 0 / A1 — telemetry roadmap.

Calling :func:`setup_observability` early in process startup wires up the
``azure-monitor-opentelemetry`` distro, which in turn enables OpenTelemetry
auto-instrumentation for:

* FastAPI (request rows in the ``requests`` Application Insights table)
* SQLAlchemy (``dependencies`` rows for every database call)
* HTTPX / requests / urllib3 (``dependencies`` rows for outbound HTTP)

Unhandled exceptions raised inside instrumented frames are also captured
into the ``exceptions`` table.  This is the whole point of also wiring the
**transcode worker** up to this bootstrap: a transcode failure (a missing
module, a render crash, a timeout) raised in the worker now surfaces in the
App Insights ``exceptions`` table and trips the fleet exception alert,
instead of dying silently in container stdout.

Pass ``role_name`` to set the ``cloud_RoleName`` dimension (via
``OTEL_SERVICE_NAME``) so CMS and worker telemetry are distinguishable in
Application Insights — e.g. the worker passes ``"agora-worker"``.  When
omitted the azure-monitor default role name is used (the CMS keeps its
existing default so dashboards / alerts that key on it are unaffected).

The function is a **no-op** when ``APPLICATIONINSIGHTS_CONNECTION_STRING``
is unset — that's the expected state for local development, docker-compose
runs, and the unit-test suite.  This means we can call it unconditionally
from ``cms.main`` (and ``worker.__main__``) without breaking anything.

It is also safe to call multiple times in the same process: the underlying
``configure_azure_monitor`` short-circuits on the second invocation.
"""

from __future__ import annotations

import logging
import os
from typing import Final

logger = logging.getLogger("agora.observability")

_CONN_STRING_ENV: Final[str] = "APPLICATIONINSIGHTS_CONNECTION_STRING"
# Honour both the historical CMS-specific kill switch and a process-neutral
# one so the worker can be silenced independently if ever needed.
_DISABLE_ENVS: Final[tuple[str, ...]] = (
    "AGORA_DISABLE_OBSERVABILITY",
    "AGORA_CMS_DISABLE_OBSERVABILITY",
)
_ROLE_NAME_ENV: Final[str] = "OTEL_SERVICE_NAME"

# Process-level flag so repeated calls don't double-initialize the
# OpenTelemetry exporter.  ``configure_azure_monitor`` itself is
# idempotent, but logging "enabled" twice is just noise.
_initialised: bool = False


def setup_observability(role_name: str | None = None) -> bool:
    """Initialise Application Insights / OpenTelemetry export.

    ``role_name`` sets the ``cloud_RoleName`` telemetry dimension so each
    process (``agora-cms`` vs ``agora-worker``) is distinguishable in
    Application Insights.  When ``None`` the azure-monitor default is used.

    Returns ``True`` when telemetry export was enabled (connection string
    present and SDK loaded successfully), ``False`` otherwise.  Callers
    don't need to inspect the return value — it's exposed mainly for
    tests.
    """
    global _initialised  # noqa: PLW0603
    if _initialised:
        return True

    disabled_via = next(
        (
            env
            for env in _DISABLE_ENVS
            if os.environ.get(env, "").lower() in {"1", "true", "yes"}
        ),
        None,
    )
    if disabled_via is not None:
        logger.info(
            "observability disabled via %s; skipping App Insights init",
            disabled_via,
        )
        return False

    conn = os.environ.get(_CONN_STRING_ENV, "").strip()
    if not conn:
        logger.info(
            "%s not set; App Insights export disabled (this is expected "
            "for local/dev runs)",
            _CONN_STRING_ENV,
        )
        return False

    try:
        # Imported lazily so the dependency is only required when
        # telemetry is actually enabled.  This keeps the unit-test
        # environment small and lets the CMS run without the SDK
        # installed (e.g. minimal local dev container).
        from azure.monitor.opentelemetry import configure_azure_monitor
    except ImportError:
        logger.warning(
            "azure-monitor-opentelemetry not installed; App Insights "
            "export disabled despite %s being set.  Install the package "
            "or unset the env var to silence this warning.",
            _CONN_STRING_ENV,
        )
        return False

    try:
        if role_name:
            # azure-monitor reads the cloud_RoleName from OTEL_SERVICE_NAME.
            # setdefault so an explicit env override (deploy-time) still wins.
            os.environ.setdefault(_ROLE_NAME_ENV, role_name)
        configure_azure_monitor(connection_string=conn)
    except Exception:  # pragma: no cover — defensive: never let a
        # telemetry init failure crash the process
        logger.exception(
            "configure_azure_monitor() failed; process will continue without "
            "telemetry export"
        )
        return False

    _initialised = True
    logger.info(
        "App Insights observability enabled (role=%s; FastAPI / SQLAlchemy / "
        "HTTPX auto-instrumentation active)",
        os.environ.get(_ROLE_NAME_ENV) or "default",
    )
    return True


def _reset_for_tests() -> None:
    """Reset the module-level "already initialised" flag.

    Test-only helper.  Production code never calls this.
    """
    global _initialised  # noqa: PLW0603
    _initialised = False
