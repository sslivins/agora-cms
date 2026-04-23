"""Multi-replica N=2 hardening: device transport must be installed BEFORE
background tasks are spawned.

Why this test exists
====================
Before this fix, ``cms/main.py`` lifespan spawned ``scheduler_loop`` and a
dozen other background tasks at startup, and only THEN resolved
``settings.device_transport`` and called ``set_transport()``.  During the
window between the first ``asyncio.create_task()`` and ``set_transport()``,
``get_transport()`` returned the module-default ``LocalDeviceTransport()``
even when running in WPS mode.

If a background loop (e.g. scheduler) fired a send attempt in that window,
``LocalDeviceTransport.send_to_device`` would unconditionally call
``device_presence.mark_offline()`` on failure — flipping healthy WPS
devices offline during cold-start in production.

The scheduler's ``LeaderLease`` usually prevented this on rolling deploys
(outgoing replica still held the lease, new replica waited ~30s before
ticking, by which time ``set_transport`` had run), but cold-cluster starts
(no prior leader) and unlucky timing could trip it.

This test encodes the invariant: **every ``asyncio.create_task`` call made
from lifespan startup must observe the installed transport, not the
module default.**  The reorder in ``cms/main.py`` makes that true; this
test will fail if anyone later moves ``set_transport()`` back below the
task creation calls.
"""

from __future__ import annotations

import asyncio
import pathlib

import pytest

from cms.models.device_event import DeviceEventType


class _FakeSettings:
    """Minimal settings object matching what lifespan touches."""
    storage_backend = "local"
    asset_storage_path = pathlib.Path(".")
    azure_storage_connection_string = None
    azure_storage_account_name = None
    azure_storage_account_key = None
    azure_sas_expiry_hours = 1
    device_transport = "local"
    wps_connection_string = None
    wps_hub = "agora"


class _FakeWPSSettings(_FakeSettings):
    device_transport = "wps"
    wps_connection_string = "Endpoint=https://fake.webpubsub.azure.com;AccessKey=ZmFrZQ=="
    wps_hub = "agora"


async def _run_lifespan_and_capture_order(
    app,
    db_session,
    monkeypatch,
    settings_cls,
):
    """Run the real lifespan while recording (kind, transport-at-call-time) tuples.

    ``kind`` is either "task" (an ``asyncio.create_task`` call made from
    lifespan setup) or "set_transport" (the transport singleton install).
    ``transport-at-call-time`` captures the concrete transport class name
    observed at each event boundary.
    """
    from cms import main as main_mod
    from cms.services import transport as transport_module

    # ── Stub heavy helpers so lifespan runs fast and deterministically ──
    async def _noop(*a, **kw):
        return None

    monkeypatch.setattr(main_mod, "_seed_profiles", _noop)
    monkeypatch.setattr(main_mod, "_backfill_media_metadata", _noop)
    monkeypatch.setattr(main_mod, "ensure_admin_credentials", _noop)
    monkeypatch.setattr(main_mod, "init_db", lambda *a, **kw: None)

    async def _noop_wait():
        return None
    monkeypatch.setattr(main_mod, "wait_for_db", _noop_wait)
    monkeypatch.setattr(main_mod, "run_migrations", _noop_wait)
    monkeypatch.setattr(main_mod, "dispose_db", _noop_wait)

    async def _idle():
        while True:
            await asyncio.sleep(3600)

    # Replace every background loop with an idle awaitable so cancellation
    # at shutdown is clean and nothing real fires during the test.
    for name in (
        "scheduler_loop",
        "version_check_loop",
        "device_purge_loop",
        "service_key_rotation_loop",
        "_alert_settings_refresh_loop",
        "stream_capture_monitor_loop",
        "deleted_asset_reaper_loop",
        "outbox_drain_loop",
        "_offline_sweep_loop",
    ):
        if hasattr(main_mod, name):
            monkeypatch.setattr(main_mod, name, _idle)

    # Stub transcoder one-shot helpers
    from cms.services import transcoder as _tc
    async def _fix_noop(_db):
        return None
    monkeypatch.setattr(_tc, "fix_image_variant_extensions", _fix_noop)

    # Stub settings
    monkeypatch.setattr(main_mod, "get_settings", lambda: settings_cls())

    # Stub WPSTransport so we don't need a real Azure WPS connection
    if settings_cls.device_transport == "wps":
        from cms.services import wps_transport as _wps_mod

        class _FakeWPS:
            def __init__(self, *a, **kw):
                pass

            async def close(self):
                pass

        monkeypatch.setattr(_wps_mod, "WPSTransport", _FakeWPS)
        # Also stub include_router so the test doesn't mutate the real app's
        # route table (router pollution across tests).
        import unittest.mock as _mock
        monkeypatch.setattr(app, "include_router", _mock.Mock())

    # ── Record transport observations at each task/set_transport boundary ──
    # We only care about tasks CREATED FROM cms/main.py lifespan — not
    # random tasks pytest-asyncio or other infra creates around us.  Filter
    # by the CALLER's file (not the coroutine's file — we've stubbed loops
    # with idle coroutines defined in this test module).
    import cms.main as _cms_main
    import sys as _sys
    cms_main_file = _cms_main.__file__

    events: list[tuple[str, str]] = []
    real_create_task = asyncio.create_task

    def recording_create_task(coro, *args, **kwargs):
        try:
            caller_frame = _sys._getframe(1)
            caller_file = caller_frame.f_code.co_filename
        except Exception:
            caller_file = ""
        if caller_file == cms_main_file:
            current = transport_module.get_transport()
            events.append(("task", type(current).__name__))
        return real_create_task(coro, *args, **kwargs)

    real_set_transport = transport_module.set_transport

    def recording_set_transport(impl):
        events.append(("set_transport", type(impl).__name__))
        real_set_transport(impl)

    # Patch both the module attribute AND the already-imported reference
    # inside main.py.  main.py does `from cms.services import transport as
    # transport_module` and then calls `transport_module.set_transport`,
    # so patching the module attribute is what takes effect.
    monkeypatch.setattr(transport_module, "set_transport", recording_set_transport)
    monkeypatch.setattr(asyncio, "create_task", recording_create_task)

    try:
        async with main_mod.lifespan(app):
            # inside startup: let it quiesce
            await asyncio.sleep(0)
    finally:
        # Restore the default transport so other tests see a clean state
        transport_module.reset_transport_to_local()

    return events


@pytest.mark.asyncio
async def test_transport_installed_before_background_tasks_in_local_mode(
    app, db_session, monkeypatch,
):
    """Local-mode lifespan: set_transport() fires before any create_task()."""
    events = await _run_lifespan_and_capture_order(
        app, db_session, monkeypatch, _FakeSettings,
    )

    assert events, "Lifespan should produce startup events"

    # Find the first set_transport and the first task
    first_set_idx = next(
        (i for i, (k, _) in enumerate(events) if k == "set_transport"), None,
    )
    first_task_idx = next(
        (i for i, (k, _) in enumerate(events) if k == "task"), None,
    )

    assert first_set_idx is not None, (
        f"set_transport must be called during lifespan startup. events={events}"
    )
    assert first_task_idx is not None, (
        f"At least one background task must be created during lifespan. events={events}"
    )
    assert first_set_idx < first_task_idx, (
        f"set_transport (idx={first_set_idx}) must precede the first "
        f"create_task (idx={first_task_idx}) to avoid a cold-start race. "
        f"events={events}"
    )

    # Every recorded task must observe the installed LocalDeviceTransport,
    # NOT some pre-install state.  The singleton starts as
    # LocalDeviceTransport() at module load, but set_transport must still
    # have run first so the invariant is explicit.
    for kind, cls_name in events:
        if kind == "task":
            assert cls_name == "LocalDeviceTransport", (
                f"Task created while transport was {cls_name!r}; expected "
                f"LocalDeviceTransport.  events={events}"
            )


@pytest.mark.asyncio
async def test_transport_installed_before_background_tasks_in_wps_mode(
    app, db_session, monkeypatch,
):
    """WPS-mode lifespan: WPSTransport is installed before any create_task().

    This is the race that caused the real bug — if scheduler or another
    loop fired while the transport was still the module-default
    LocalDeviceTransport, it would flip WPS devices offline on send
    failure.  This test enforces the ordering.
    """
    events = await _run_lifespan_and_capture_order(
        app, db_session, monkeypatch, _FakeWPSSettings,
    )

    # Find first set_transport and first task
    first_set_idx = next(
        (i for i, (k, _) in enumerate(events) if k == "set_transport"), None,
    )
    first_task_idx = next(
        (i for i, (k, _) in enumerate(events) if k == "task"), None,
    )

    assert first_set_idx is not None, (
        f"set_transport must be called during lifespan startup. events={events}"
    )
    assert first_task_idx is not None, (
        f"At least one background task must be created during lifespan. events={events}"
    )
    assert first_set_idx < first_task_idx, (
        f"set_transport (idx={first_set_idx}) must precede the first "
        f"create_task (idx={first_task_idx}) in WPS mode — otherwise "
        f"tasks observe the module-default LocalDeviceTransport during "
        f"cold start, which can flip healthy WPS devices offline. "
        f"events={events}"
    )

    # Every recorded task must observe a real WPSTransport (our fake),
    # NOT a LocalDeviceTransport.  This is the strong form of the
    # invariant — weaker "set_transport came first" is already checked above.
    set_idx = first_set_idx
    installed_cls = events[set_idx][1]
    for i, (kind, cls_name) in enumerate(events):
        if kind == "task":
            assert cls_name == installed_cls, (
                f"Task at event-index {i} observed transport {cls_name!r}, "
                f"expected {installed_cls!r} (the installed WPS transport). "
                f"events={events}"
            )
