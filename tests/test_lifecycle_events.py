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
  - intermediate OTA phase events (signature_verified / staged /
    tryboot_initiated / slot_confirmed / promoted) project to the
    device but skip the audit row — the event log only surfaces
    "upgrade started", "upgrade completed", and failure modes.
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
async def test_lifecycle_intermediate_phase_events_skip_audit_row(
    app, _device,
):
    """Intermediate OTA phase events project to the device but skip audit.

    ``signature_verified`` / ``staged`` / ``tryboot_initiated`` /
    ``slot_confirmed`` / ``promoted`` are debugging-grade detail — a
    user scanning the event log only cares about "upgrade started",
    "upgrade completed" (``migration_complete``), and failure modes.
    The projection still runs so the live device badge updates.
    """
    intermediate_types = [
        ("signature_verified", DeviceEventType.OTA_SIGNATURE_VERIFIED),
        ("staged", DeviceEventType.OTA_STAGED),
        ("tryboot_initiated", DeviceEventType.OTA_TRYBOOT_INITIATED),
        ("slot_confirmed", DeviceEventType.OTA_SLOT_CONFIRMED),
        ("promoted", DeviceEventType.OTA_PROMOTED),
    ]

    for idx, (wire, _cms) in enumerate(intermediate_types, start=20):
        msg = {
            "type": MessageType.LIFECYCLE_EVENT.value, "event_id": idx,
            "event_type": wire, "payload": {},
        }
        await _dispatch(_device, msg, app)

    # No audit rows for any intermediate phase.
    for _wire, cms_type in intermediate_types:
        rows = await _read_events(_device, cms_type.value)
        assert rows == [], (
            f"{cms_type.value} should be suppressed but found {len(rows)} row(s)"
        )

    # ``promoted`` is terminal so the projection clears ota_phase.  Verify
    # the projection actually ran for at least one intermediate event by
    # checking a non-terminal one mid-sequence — re-drive ``staged`` and
    # confirm the projection lands.
    msg = {
        "type": MessageType.LIFECYCLE_EVENT.value, "event_id": 50,
        "event_type": "staged", "payload": {},
    }
    await _dispatch(_device, msg, app)
    dev = await _read_device(_device)
    assert dev.ota_phase == DeviceEventType.OTA_STAGED.value

    # Still no audit row even after re-dispatch.
    rows = await _read_events(_device, DeviceEventType.OTA_STAGED.value)
    assert rows == []
