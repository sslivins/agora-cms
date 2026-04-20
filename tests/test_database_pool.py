"""Engine-config tests for shared.database.

Regression coverage for the v1.37.19 incident: a Postgres restart left
the SQLAlchemy pool holding closed connections.  Without ``pool_pre_ping``,
the next checkout raised ``InterfaceError: connection is closed`` and (in
the worker's listen loop) crashed the entire process.

These tests pin the pool-resilience knobs so a future refactor can't
silently regress us into the same incident.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


def test_init_db_enables_pool_pre_ping_and_recycle():
    """``init_db`` must configure ``pool_pre_ping=True`` AND ``pool_recycle``.

    pool_pre_ping issues a cheap SELECT 1 before handing out a pooled
    connection — catches connections invalidated by Postgres restart, idle
    timeout, network blip, or Azure managed-DB failover.

    pool_recycle proactively retires connections older than N seconds so
    we don't accumulate ones the server has already closed silently.
    """
    from shared import database as shared_db

    captured = {}

    def _spy_create_async_engine(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return MagicMock()

    settings = MagicMock()
    settings.database_url = "postgresql+asyncpg://u:p@host/db"

    with patch.object(shared_db, "create_async_engine", side_effect=_spy_create_async_engine), \
         patch.object(shared_db, "async_sessionmaker", return_value=MagicMock()):
        shared_db.init_db(settings)

    kwargs = captured["kwargs"]
    assert kwargs.get("pool_pre_ping") is True, (
        "shared.database.init_db must pass pool_pre_ping=True to "
        "create_async_engine — without it, a stale pool connection after "
        "a Postgres restart raises InterfaceError on next checkout. "
        "See v1.37.19 incident."
    )
    assert isinstance(kwargs.get("pool_recycle"), int) and kwargs["pool_recycle"] > 0, (
        "shared.database.init_db must pass a positive pool_recycle to "
        "create_async_engine to retire stale connections proactively."
    )
    # Recycle must be reasonably short — 5 minutes is the chosen default;
    # anything longer than an hour defeats the purpose of the safety net.
    assert kwargs["pool_recycle"] <= 3600, (
        f"pool_recycle={kwargs['pool_recycle']}s is too long; staleness "
        f"safety net should fire within an hour."
    )


# ── Compose hardening ────────────────────────────────────────────────────


def test_docker_compose_long_lived_services_have_restart_policy():
    """Every long-lived service in docker-compose.yml must declare ``restart``.

    A missing restart policy is what turned the v1.37.19 worker crash into a
    multi-hour outage: the container exited 1 and never came back.  Pin this
    so a refactor can't reintroduce the gap.
    """
    from pathlib import Path

    import yaml  # PyYAML — already a transitive of the project

    compose_path = Path(__file__).resolve().parents[1] / "docker-compose.yml"
    spec = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    services = spec.get("services") or {}
    # All services in the base compose are long-lived (no one-shot helpers
    # in here today).  If you add one, exclude it explicitly.
    missing = [
        name for name, cfg in services.items()
        if not (cfg or {}).get("restart")
    ]
    assert not missing, (
        f"docker-compose.yml services without a restart policy: {missing}. "
        f"Add `restart: unless-stopped` so a transient crash doesn't turn "
        f"into a permanent outage."
    )
