"""Tests for CMS_STARTED / CMS_STOPPED lifespan events and nullable device_id."""

import uuid

import pytest
from sqlalchemy import select

from cms.models.device_event import DeviceEvent, DeviceEventType


# ── Enum exposure ──


def test_deviceeventtype_enum_exposes_cms_started_stopped():
    assert DeviceEventType.CMS_STARTED.value == "cms_started"
    assert DeviceEventType.CMS_STOPPED.value == "cms_stopped"


# ── Nullable device_id (migration behavior) ──


@pytest.mark.asyncio
async def test_device_event_allows_null_device_id(db_session):
    """Insert a DeviceEvent with device_id=None and verify the DB accepts it."""
    evt = DeviceEvent(
        device_id=None,
        device_name="CMS",
        group_id=None,
        group_name="",
        event_type=DeviceEventType.CMS_STARTED,
        details={"version": "9.9.9"},
    )
    db_session.add(evt)
    await db_session.commit()
    await db_session.refresh(evt)

    assert evt.id is not None
    assert evt.device_id is None
    assert evt.event_type == DeviceEventType.CMS_STARTED
    assert evt.details == {"version": "9.9.9"}


# ── Lifespan behavior ──
#
# Note: the conftest test fixture REPLACES the real lifespan with a no-op
# (see `_test_lifespan` in tests/conftest.py) to avoid pulling in PostgreSQL,
# scheduler tasks, etc. during unit tests. That means the real startup event
# insertion is not exercised via TestClient. We test it by directly invoking
# the real lifespan context with a hand-wired DB override.


@pytest.mark.asyncio
async def test_real_lifespan_logs_cms_started_event(app, db_session, monkeypatch):
    """Run the real lifespan startup manually and check a CMS_STARTED row exists."""
    from cms import main as main_mod
    from cms.database import get_db

    # Stub out all the heavy startup helpers so we only exercise event logging.
    async def _noop(*args, **kwargs):
        return None

    async def _noop_gen(*a, **kw):
        if False:
            yield
        return

    monkeypatch.setattr(main_mod, "_seed_roles", _noop)
    monkeypatch.setattr(main_mod, "_seed_profiles", _noop)
    monkeypatch.setattr(main_mod, "_migrate_variant_filenames", _noop)
    monkeypatch.setattr(main_mod, "_backfill_media_metadata", _noop)
    monkeypatch.setattr(main_mod, "ensure_admin_credentials", _noop)
    monkeypatch.setattr(main_mod, "init_db", lambda *_a, **_kw: None)

    async def _noop_wait():
        return None
    monkeypatch.setattr(main_mod, "wait_for_db", _noop_wait)

    async def _noop_migrate():
        return None
    monkeypatch.setattr(main_mod, "run_migrations", _noop_migrate)

    async def _noop_dispose():
        return None
    monkeypatch.setattr(main_mod, "dispose_db", _noop_dispose)

    # Replace scheduler/background loops with no-op awaitables so we can cancel them cleanly.
    async def _idle():
        import asyncio
        while True:
            await asyncio.sleep(3600)

    monkeypatch.setattr(main_mod, "scheduler_loop", _idle)
    monkeypatch.setattr(main_mod, "version_check_loop", _idle)
    monkeypatch.setattr(main_mod, "device_purge_loop", _idle)
    monkeypatch.setattr(main_mod, "service_key_rotation_loop", _idle)
    monkeypatch.setattr(main_mod, "_alert_settings_refresh_loop", _idle)

    # Also stub transcoder helper
    from cms.services import transcoder as _tc
    async def _fix_noop(_db):
        return None
    monkeypatch.setattr(_tc, "fix_image_variant_extensions", _fix_noop)

    # Make get_settings return a minimal object with the attrs lifespan touches
    class _S:
        storage_backend = "local"
        asset_storage_path = __import__("pathlib").Path(".")
        azure_storage_connection_string = None
        azure_storage_account_name = None
        azure_storage_account_key = None
        azure_sas_expiry_hours = 1
    monkeypatch.setattr(main_mod, "get_settings", lambda: _S())

    # init_storage no-op already done by the app fixture (it initializes local storage).

    # Run the real lifespan and yield briefly
    async with main_mod.lifespan(app):
        # Inside: startup has run; CMS_STARTED should now be in the DB.
        result = await db_session.execute(
            select(DeviceEvent).where(
                DeviceEvent.event_type == DeviceEventType.CMS_STARTED,
                DeviceEvent.device_id.is_(None),
            )
        )
        events = result.scalars().all()
        assert len(events) >= 1
        evt = events[-1]
        assert evt.device_id is None
        assert evt.details is not None
        assert "version" in evt.details

    # After the context exits, CMS_STOPPED should also be logged.
    result = await db_session.execute(
        select(DeviceEvent).where(
            DeviceEvent.event_type == DeviceEventType.CMS_STOPPED,
            DeviceEvent.device_id.is_(None),
        )
    )
    stopped = result.scalars().all()
    assert len(stopped) >= 1
    assert "version" in stopped[-1].details
