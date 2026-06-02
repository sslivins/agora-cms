"""Integration tests for the OTA ``lifecycle_event`` branch in
``cms.services.device_inbound.dispatch_device_message`` (issue
agora-cms#574 / agora#215).

Pattern mirrors ``tests/test_logs_response_shim.py``: drive the
dispatcher directly with a synthetic message, then re-open the
session to verify the row + audit event landed.

Covers:
  - happy path: a known event_type updates ``devices.ota_*`` AND
    writes a ``device_events`` audit row.
  - unknown wire event_type: dropped with INFO log, no audit row,
    no Device mutation, no commit.
  - terminal event clears ``upgrade_started_at`` end-to-end through
    the dispatch path (this is the failure-path UX fix — without it
    a failed OTA would leave the "Upgrading…" badge stuck for the
    full ``UPGRADE_TTL=15min``).
  - regression-dropped event STILL writes an audit row with
    ``projection_applied: false`` so the device's monotonic event_id
    is accounted for in the event log.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select

from cms.database import get_session_factory
from cms.models.device import Device, DeviceStatus
from cms.models.device_event import DeviceEvent, DeviceEventType
from cms.schemas.protocol import MessageType
from cms.services.device_inbound import InboundContext, dispatch_device_message


async def _noop_send(msg: dict) -> None:
    return None


@pytest_asyncio.fixture
async def _device(app):
    factory = get_session_factory()
    async with factory() as db:
        dev = Device(
            id="dev-ota-1", name="OTA",
            status=DeviceStatus.ADOPTED,
            upgrade_started_at=datetime.now(timezone.utc),
        )
        db.add(dev)
        await db.commit()
    return "dev-ota-1"


async def _dispatch(_device, msg, app):
    """Helper — open a session, build context, dispatch one message."""
    from cms.auth import get_settings
    settings = app.dependency_overrides[get_settings]()
    factory = get_session_factory()
    async with factory() as db:
        device = await db.get(Device, _device)
        ctx = InboundContext(
            device_id=_device, device=device,
            device_name=device.name,
            base_url="http://test", settings=settings,
            group_id=None, group_name=None,
            device_status=device.status,
        )
        await dispatch_device_message(msg=msg, ctx=ctx, db=db, send=_noop_send)


async def _read_device(_device):
    factory = get_session_factory()
    async with factory() as db:
        return await db.get(Device, _device)


async def _read_events(_device, event_type=None):
    factory = get_session_factory()
    async with factory() as db:
        q = select(DeviceEvent).where(DeviceEvent.device_id == _device)
        if event_type is not None:
            q = q.where(DeviceEvent.event_type == event_type)
        result = await db.execute(q)
        return result.scalars().all()


@pytest.mark.asyncio
async def test_lifecycle_download_progress_updates_device_but_skips_audit_row(
    app, _device,
):
    """Progress events project to the device but DO NOT persist a DeviceEvent.

    Progress messages arrive every few seconds during an OTA and add
    no audit value over the surrounding ``*_STARTED`` / ``STAGED`` /
    ``SLOT_CONFIRMED`` rows — they would just spam the event log.
    The live progress badge still updates via the projection.
    """
    msg = {
        "type": MessageType.LIFECYCLE_EVENT.value,
        "event_id": 42,
        "event_type": "download_progress",
        "release_id": "rel-v0.0.30-test",
        "target_version": "0.0.30-test",
        "occurred_at": "2026-05-16T07:30:00Z",
        "payload": {"bytes_done": 250, "bytes_total": 1000},
    }
    await _dispatch(_device, msg, app)

    dev = await _read_device(_device)
    assert dev.ota_phase == DeviceEventType.OTA_DOWNLOAD_PROGRESS.value
    assert dev.ota_pct == pytest.approx(25.0)
    assert dev.ota_bytes_done == 250
    assert dev.ota_bytes_total == 1000
    assert dev.ota_updated_at is not None
    # The atomic upgrade claim is NOT cleared (still in flight).
    assert dev.upgrade_started_at is not None

    # No audit row for progress events — explicitly suppressed.
    events = await _read_events(_device,
                                DeviceEventType.OTA_DOWNLOAD_PROGRESS.value)
    assert events == []


@pytest.mark.asyncio
async def test_lifecycle_unknown_event_type_is_silently_dropped(app, _device):
    # An event_type the CMS doesn't know about (added later in agora
    # but not yet in this CMS deploy) must be a no-op — no audit row,
    # no Device mutation, no exception.  Forward-compat is mandatory
    # because agora ships independently of agora-cms.
    msg = {
        "type": MessageType.LIFECYCLE_EVENT.value,
        "event_id": 99,
        "event_type": "brand_new_event_invented_in_v2",
        "payload": {"shiny": True},
    }
    await _dispatch(_device, msg, app)

    dev = await _read_device(_device)
    assert dev.ota_phase is None
    assert dev.ota_label is None
    # upgrade_started_at left as fixture put it (still set).
    assert dev.upgrade_started_at is not None

    events = await _read_events(_device)
    assert events == []


@pytest.mark.asyncio
async def test_lifecycle_terminal_event_clears_upgrade_claim(app, _device):
    # Pre-condition: device is mid-OTA so ota_* columns are set AND
    # upgrade_started_at is set (fixture sets the latter).
    setup_msg = {
        "type": MessageType.LIFECYCLE_EVENT.value, "event_id": 1,
        "event_type": "stage_progress",
        "payload": {"phase": "extracting_meta"},
    }
    await _dispatch(_device, setup_msg, app)

    # Sanity check that we set up a "mid-OTA" state.
    mid = await _read_device(_device)
    assert mid.ota_phase == DeviceEventType.OTA_STAGE_PROGRESS.value
    assert mid.upgrade_started_at is not None

    # Now a terminal failure event arrives.
    fail_msg = {
        "type": MessageType.LIFECYCLE_EVENT.value, "event_id": 2,
        "event_type": "failed",
        "reason": "signature_mismatch",
        "payload": {},
    }
    await _dispatch(_device, fail_msg, app)

    dev = await _read_device(_device)
    # All ota_* fields cleared.
    assert dev.ota_phase is None
    assert dev.ota_label is None
    assert dev.ota_pct is None
    assert dev.ota_bytes_done is None
    assert dev.ota_bytes_total is None
    # The atomic upgrade claim is ALSO cleared so the badge falls off
    # immediately on failure rather than hanging for UPGRADE_TTL.
    assert dev.upgrade_started_at is None

    # Audit row carries the reason for postmortem use.
    events = await _read_events(_device, DeviceEventType.OTA_FAILED.value)
    assert len(events) == 1
    assert events[0].details["reason"] == "signature_mismatch"


@pytest.mark.asyncio
async def test_lifecycle_regression_dropped_event_audit_policy(app, _device):
    """Non-progress regression events still write an audit row.

    A regression-dropped transition event still persists with
    ``projection_applied=False`` so the audit trail is complete.
    Progress events are suppressed regardless — see
    ``test_lifecycle_download_progress_updates_device_but_skips_audit_row``.
    """
    # First event lands.
    forward = {
        "type": MessageType.LIFECYCLE_EVENT.value, "event_id": 10,
        "event_type": "slot_confirmed", "payload": {},
    }
    await _dispatch(_device, forward, app)

    dev_after_forward = await _read_device(_device)
    assert dev_after_forward.ota_phase == DeviceEventType.OTA_SLOT_CONFIRMED.value

    # A regression non-progress event arrives (staged, ordinal lower
    # than slot_confirmed).  Projection refuses the regression but we
    # still write an audit row so the device's monotonic event_id is
    # accounted for in the event log.
    regression = {
        "type": MessageType.LIFECYCLE_EVENT.value, "event_id": 11,
        "event_type": "staged",
        "payload": {},
    }
    await _dispatch(_device, regression, app)

    dev_after = await _read_device(_device)
    # Device state unchanged — slot_confirmed still wins.
    assert dev_after.ota_phase == DeviceEventType.OTA_SLOT_CONFIRMED.value

    regr_events = await _read_events(
        _device, DeviceEventType.OTA_STAGED.value,
    )
    assert len(regr_events) == 1
    assert regr_events[0].details["event_id"] == 11
    assert regr_events[0].details["projection_applied"] is False

    # And a regression progress event is suppressed — no audit row.
    progress_regression = {
        "type": MessageType.LIFECYCLE_EVENT.value, "event_id": 12,
        "event_type": "download_progress",
        "payload": {"bytes_done": 50, "bytes_total": 1000},
    }
    await _dispatch(_device, progress_regression, app)

    prog_events = await _read_events(
        _device, DeviceEventType.OTA_DOWNLOAD_PROGRESS.value,
    )
    assert prog_events == []
