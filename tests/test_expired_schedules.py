"""Tests for expired schedules panel on the schedules page."""

from datetime import datetime, time, timedelta, timezone

import pytest
import pytest_asyncio

from cms.models.asset import Asset, AssetType
from cms.models.device import Device, DeviceStatus
from cms.models.schedule import Schedule


@pytest.mark.asyncio
class TestExpiredSchedulesPage:

    async def _seed(self, db_session):
        """Create a device + asset and return their IDs."""
        device = Device(id="exp-pi", name="Expired Test", status=DeviceStatus.ADOPTED)
        asset = Asset(filename="clip.mp4", asset_type=AssetType.VIDEO, size_bytes=100, checksum="abc")
        db_session.add_all([device, asset])
        await db_session.commit()
        return device.id, asset.id

    async def _create_schedule(self, db_session, *, name, device_id, asset_id,
                                start_date=None, end_date=None, enabled=True):
        sched = Schedule(
            name=name,
            device_id=device_id,
            asset_id=asset_id,
            start_time=time(9, 0),
            end_time=time(17, 0),
            start_date=start_date,
            end_date=end_date,
            enabled=enabled,
        )
        db_session.add(sched)
        await db_session.commit()
        return sched

    async def test_expired_schedule_in_expired_panel(self, client, db_session):
        """A schedule whose end_date is in the past should appear in 'Expired Schedules'."""
        device_id, asset_id = await self._seed(db_session)
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)
        await self._create_schedule(
            db_session, name="Old Promo", device_id=device_id, asset_id=asset_id,
            start_date=yesterday - timedelta(days=7), end_date=yesterday,
        )

        resp = await client.get("/schedules")
        assert resp.status_code == 200
        html = resp.text
        assert "Expired Schedules" in html
        assert "Old Promo" in html

    async def test_active_schedule_not_in_expired_panel(self, client, db_session):
        """A schedule with no end_date should only appear in 'Active Schedules'."""
        device_id, asset_id = await self._seed(db_session)
        await self._create_schedule(
            db_session, name="Evergreen", device_id=device_id, asset_id=asset_id,
        )

        resp = await client.get("/schedules")
        assert resp.status_code == 200
        html = resp.text
        assert "Evergreen" in html
        assert "Expired Schedules" not in html

    async def test_future_end_date_stays_active(self, client, db_session):
        """A schedule whose end_date is in the future stays in Active Schedules."""
        device_id, asset_id = await self._seed(db_session)
        tomorrow = datetime.now(timezone.utc) + timedelta(days=1)
        await self._create_schedule(
            db_session, name="Still Running", device_id=device_id, asset_id=asset_id,
            start_date=datetime.now(timezone.utc), end_date=tomorrow,
        )

        resp = await client.get("/schedules")
        assert resp.status_code == 200
        html = resp.text
        assert "Still Running" in html
        assert "Expired Schedules" not in html

    async def test_both_active_and_expired(self, client, db_session):
        """When both active and expired schedules exist, both panels appear."""
        device_id, asset_id = await self._seed(db_session)
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)

        await self._create_schedule(
            db_session, name="Current Campaign", device_id=device_id, asset_id=asset_id,
        )
        await self._create_schedule(
            db_session, name="Past Event", device_id=device_id, asset_id=asset_id,
            start_date=yesterday - timedelta(days=3), end_date=yesterday,
        )

        resp = await client.get("/schedules")
        assert resp.status_code == 200
        html = resp.text
        assert "Active Schedules" in html
        assert "Expired Schedules" in html
        assert "Current Campaign" in html
        assert "Past Event" in html

    async def test_expired_panel_hidden_when_no_expired(self, client, db_session):
        """The expired panel should not render when there are no expired schedules."""
        device_id, asset_id = await self._seed(db_session)
        await self._create_schedule(
            db_session, name="Active Only", device_id=device_id, asset_id=asset_id,
        )

        resp = await client.get("/schedules")
        html = resp.text
        assert "Expired Schedules" not in html

    async def test_expired_schedule_has_edit_button(self, client, db_session):
        """Expired schedules should still have an Edit button so they can be reactivated."""
        device_id, asset_id = await self._seed(db_session)
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)
        sched = await self._create_schedule(
            db_session, name="Reactivatable", device_id=device_id, asset_id=asset_id,
            start_date=yesterday - timedelta(days=1), end_date=yesterday,
        )

        resp = await client.get("/schedules")
        html = resp.text
        assert "Reactivatable" in html
        assert f"editSchedule(" in html
