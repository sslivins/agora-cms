"""Application Insights / OpenTelemetry bootstrap for the CMS.

Issue #474, Phase 0 / A1 — telemetry roadmap.

Calling :func:`setup_observability` early in process startup wires up the
``azure-monitor-opentelemetry`` distro, which in turn enables OpenTelemetry
auto-instrumentation for:

* FastAPI (request rows in the ``requests`` Application Insights table)
* SQLAlchemy (``dependencies`` rows for every database call)
* HTTPX / requests / urllib3 (``dependencies`` rows for outbound HTTP)

Unhandled exceptions raised inside instrumented frames are also captured
into the ``exceptions`` table.

The function is a **no-op** when ``APPLICATIONINSIGHTS_CONNECTION_STRING``
is unset — that's the expected state for local development, docker-compose
runs, and the unit-test suite.  This means we can call it unconditionally
from ``cms.main`` without breaking anything.

It is also safe to call multiple times in the same process: the underlying
``configure_azure_monitor`` short-circuits on the second invocation.
"""

from __future__ import annotations

import logging
import os
from typing import Final

logger = logging.getLogger("agora.cms.observability")

_CONN_STRING_ENV: Final[str] = "APPLICATIONINSIGHTS_CONNECTION_STRING"
_DISABLE_ENV: Final[str] = "AGORA_CMS_DISABLE_OBSERVABILITY"

# Process-level flag so repeated calls don't double-initialize the
# OpenTelemetry exporter.  ``configure_azure_monitor`` itself is
# idempotent, but logging "enabled" twice is just noise.
_initialised: bool = False


def setup_observability() -> bool:
    """Initialise Application Insights / OpenTelemetry export.

    Returns ``True`` when telemetry export was enabled (connection string
    present and SDK loaded successfully), ``False`` otherwise.  Callers
    don't need to inspect the return value — it's exposed mainly for
    tests.
    """
    global _initialised  # noqa: PLW0603
    if _initialised:
        return True

    if os.environ.get(_DISABLE_ENV, "").lower() in {"1", "true", "yes"}:
        logger.info(
            "observability disabled via %s; skipping App Insights init",
            _DISABLE_ENV,
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
        configure_azure_monitor(connection_string=conn)
    except Exception:  # pragma: no cover — defensive: never let a
        # telemetry init failure crash the CMS process
        logger.exception(
            "configure_azure_monitor() failed; CMS will continue without "
            "telemetry export"
        )
        return False

    _initialised = True
    logger.info(
        "App Insights observability enabled (FastAPI / SQLAlchemy / "
        "HTTPX auto-instrumentation active)"
    )
    return True


def _reset_for_tests() -> None:
    """Reset the module-level "already initialised" flag.

    Test-only helper.  Production code never calls this.
    """
    global _initialised  # noqa: PLW0603
    _initialised = False
