"""Tests for the startup-time stuck-OTA projection backfill.

Covers ``cms.main._backfill_stuck_ota_projection`` — the one-shot
sweep that fixes the "Upgrade stalled" badge on devices whose firmware
completed an OTA at the hardware level but never emitted the terminal
lifecycle event (legacy updater + Mia's Pi5 class).

These tests run against the real Postgres test DB via the shared
``db_session`` fixture; the function's own ``async for db in get_db()``
generator yields a fresh session from the same engine, so committing
in ``db_session`` is visible to the function under test.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select


@pytest.fixture
def use_test_engine_globally(app):
    """Use the ``app`` fixture so ``cms.database`` / ``shared.database``
    globals point at the test engine — the backfill function calls
    ``get_db()`` and ``session_advisory_lock()`` directly, both of
    which resolve through those module-level singletons.

    We don't need the FastAPI client itself; ``app`` is just the
    cleanest way to get the engines wired in.
    """
    yield


@pytest.mark.asyncio
@pytest.mark.usefixtures("use_test_engine_globally")
class TestBackfillStuckOtaProjection:
    async def _add_device(
        self,
        db,
        *,
        device_id,
        name="Test Pi",
        ota_phase="ota_tryboot_initiated",
        ota_updated_at_offset_min=-30,
        last_seen_offset_min=-10,
        ota_label="Rebooting into new slot",
    ):
        """Insert a Device row with configurable OTA-projection state.

        Offsets are relative to ``now`` in minutes (negative = past).
        ``ota_updated_at_offset_min=None`` → leave NULL (no in-flight OTA).
        ``last_seen_offset_min=None``       → leave NULL (never reached us).
        """
        from cms.models.device import Device, DeviceStatus

        now = datetime.now(timezone.utc)
        device = Device(
            id=device_id,
            name=name,
            status=DeviceStatus.ADOPTED,
            ota_phase=ota_phase,
            ota_label=ota_label,
            ota_updated_at=(
                now + timedelta(minutes=ota_updated_at_offset_min)
                if ota_updated_at_offset_min is not None
                else None
            ),
            last_seen=(
                now + timedelta(minutes=last_seen_offset_min)
                if last_seen_offset_min is not None
                else None
            ),
        )
        db.add(device)
        return device

    async def test_clears_stuck_online_device(self, db_session):
        """Mia's case: stuck > 15 min, last_seen advanced > 5 min after
        ota_updated_at → clear projection + emit OTA_AUTO_CLEARED."""
        from cms.main import _backfill_stuck_ota_projection
        from cms.models.device import Device
        from cms.models.device_event import DeviceEvent, DeviceEventType

        await self._add_device(
            db_session,
            device_id="backfill-mia",
            name="Mia's Pi5",
            ota_updated_at_offset_min=-30,
            last_seen_offset_min=-1,
        )
        await db_session.commit()

        await _backfill_stuck_ota_projection(settings=None)

        db_session.expire_all()
        device = (await db_session.execute(
            select(Device).where(Device.id == "backfill-mia")
        )).scalar_one()
        assert device.ota_phase is None
        assert device.ota_label is None
        assert device.ota_updated_at is None

        events = (await db_session.execute(
            select(DeviceEvent).where(
                DeviceEvent.device_id == "backfill-mia",
                DeviceEvent.event_type == DeviceEventType.OTA_AUTO_CLEARED,
            )
        )).scalars().all()
        assert len(events) == 1
        evt = events[0]
        assert evt.device_name == "Mia's Pi5"
        assert evt.details["reason"] == "startup_backfill"
        assert evt.details["prior_phase"] == "ota_tryboot_initiated"
        assert evt.details["prior_ota_updated_at"] is not None
        assert evt.details["last_seen"] is not None

    async def test_skips_recently_stuck_device(self, db_session):
        """Stuck < 15 min: in-flight tryboot reboot window — leave alone."""
        from cms.main import _backfill_stuck_ota_projection
        from cms.models.device import Device

        await self._add_device(
            db_session,
            device_id="backfill-fresh",
            ota_updated_at_offset_min=-5,
            last_seen_offset_min=-1,
        )
        await db_session.commit()

        await _backfill_stuck_ota_projection(settings=None)

        db_session.expire_all()
        device = (await db_session.execute(
            select(Device).where(Device.id == "backfill-fresh")
        )).scalar_one()
        assert device.ota_phase == "ota_tryboot_initiated"

    async def test_skips_offline_stuck_device(self, db_session):
        """Stuck > 15 min but last_seen NOT advanced past ota_updated_at:
        device really is dead-mid-tryboot — leave the badge."""
        from cms.main import _backfill_stuck_ota_projection
        from cms.models.device import Device

        # ota_updated_at 30 min ago, last_seen 35 min ago (BEFORE the OTA
        # event) → device hasn't reconnected since the reboot.
        await self._add_device(
            db_session,
            device_id="backfill-offline",
            ota_updated_at_offset_min=-30,
            last_seen_offset_min=-35,
        )
        await db_session.commit()

        await _backfill_stuck_ota_projection(settings=None)

        db_session.expire_all()
        device = (await db_session.execute(
            select(Device).where(Device.id == "backfill-offline")
        )).scalar_one()
        assert device.ota_phase == "ota_tryboot_initiated"

    async def test_skips_other_ota_phases(self, db_session):
        """Mid-OTA download/verify/extract — leave projection alone."""
        from cms.main import _backfill_stuck_ota_projection
        from cms.models.device import Device

        await self._add_device(
            db_session,
            device_id="backfill-downloading",
            ota_phase="ota_downloading",
            ota_label="Downloading bundle",
            ota_updated_at_offset_min=-30,
            last_seen_offset_min=-1,
        )
        await db_session.commit()

        await _backfill_stuck_ota_projection(settings=None)

        db_session.expire_all()
        device = (await db_session.execute(
            select(Device).where(Device.id == "backfill-downloading")
        )).scalar_one()
        assert device.ota_phase == "ota_downloading"

    async def test_idempotent_on_rerun(self, db_session):
        """Second invocation is a no-op — no duplicate audit events."""
        from cms.main import _backfill_stuck_ota_projection
        from cms.models.device import Device
        from cms.models.device_event import DeviceEvent, DeviceEventType

        await self._add_device(
            db_session,
            device_id="backfill-idemp",
            ota_updated_at_offset_min=-30,
            last_seen_offset_min=-1,
        )
        await db_session.commit()

        await _backfill_stuck_ota_projection(settings=None)
        await _backfill_stuck_ota_projection(settings=None)

        db_session.expire_all()
        events = (await db_session.execute(
            select(DeviceEvent).where(
                DeviceEvent.device_id == "backfill-idemp",
                DeviceEvent.event_type == DeviceEventType.OTA_AUTO_CLEARED,
            )
        )).scalars().all()
        assert len(events) == 1

    async def test_clears_multiple_devices_in_one_pass(self, db_session):
        """Bulk path: two stuck-recoverable + one mid-download — only the
        two are cleared, each gets its own audit event."""
        from cms.main import _backfill_stuck_ota_projection
        from cms.models.device import Device
        from cms.models.device_event import DeviceEvent, DeviceEventType

        await self._add_device(
            db_session, device_id="bulk-1", name="Pi One",
            ota_updated_at_offset_min=-30, last_seen_offset_min=-1,
        )
        await self._add_device(
            db_session, device_id="bulk-2", name="Pi Two",
            ota_updated_at_offset_min=-45, last_seen_offset_min=-2,
        )
        await self._add_device(
            db_session, device_id="bulk-skip", name="Pi Mid-OTA",
            ota_phase="ota_extracting",
            ota_updated_at_offset_min=-30, last_seen_offset_min=-1,
        )
        await db_session.commit()

        await _backfill_stuck_ota_projection(settings=None)

        db_session.expire_all()
        devices = {
            d.id: d for d in (await db_session.execute(
                select(Device).where(Device.id.in_(["bulk-1", "bulk-2", "bulk-skip"]))
            )).scalars().all()
        }
        assert devices["bulk-1"].ota_phase is None
        assert devices["bulk-2"].ota_phase is None
        assert devices["bulk-skip"].ota_phase == "ota_extracting"

        events = (await db_session.execute(
            select(DeviceEvent).where(
                DeviceEvent.event_type == DeviceEventType.OTA_AUTO_CLEARED,
                DeviceEvent.device_id.in_(["bulk-1", "bulk-2", "bulk-skip"]),
            )
        )).scalars().all()
        assert {e.device_id for e in events} == {"bulk-1", "bulk-2"}
