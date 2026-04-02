"""Tests for expired schedules panel on the schedules page."""

from datetime import datetime, time, timedelta, timezone

import pytest
import pytest_asyncio

from cms.auth import set_setting, SETTING_TIMEZONE
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
                                start_time=time(9, 0), end_time=time(17, 0),
                                start_date=None, end_date=None, enabled=True):
        sched = Schedule(
            name=name,
            device_id=device_id,
            asset_id=asset_id,
            start_time=start_time,
            end_time=end_time,
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

    async def test_expired_utc_midnight_behind_utc_timezone(self, client, db_session):
        """end_date at UTC midnight that falls on the previous local day should be expired.

        Scenario: CMS timezone is America/Los_Angeles (UTC-7).
        end_date is stored as 2026-04-02 00:00:00+00 (midnight UTC).
        In PDT that's 2026-04-01 17:00:00 — the local date is April 1st.
        If today is April 2nd local, this schedule is expired.
        """
        from unittest.mock import patch
        device_id, asset_id = await self._seed(db_session)
        await set_setting(db_session, SETTING_TIMEZONE, "America/Los_Angeles")

        # end_date at UTC midnight = April 1 in PDT
        await self._create_schedule(
            db_session, name="UTC Midnight Trap", device_id=device_id, asset_id=asset_id,
            start_date=datetime(2026, 4, 1, 0, 0, tzinfo=timezone.utc),
            end_date=datetime(2026, 4, 2, 0, 0, tzinfo=timezone.utc),
        )

        # Mock "now" to April 2 at noon PDT (19:00 UTC)
        fake_now = datetime(2026, 4, 2, 19, 0, 0, tzinfo=timezone.utc)
        with patch("cms.ui.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            resp = await client.get("/schedules")

        assert resp.status_code == 200
        html = resp.text
        assert "Expired Schedules" in html
        assert "UTC Midnight Trap" in html

    async def test_same_day_expired_when_time_window_passed(self, client, db_session):
        """Schedule ending today whose time window has closed should be expired.

        end_date is today, time window 07:25-07:36, current time is 15:00.
        The schedule will never run again.
        """
        from unittest.mock import patch
        device_id, asset_id = await self._seed(db_session)
        await set_setting(db_session, SETTING_TIMEZONE, "America/Los_Angeles")

        await self._create_schedule(
            db_session, name="Morning Flash", device_id=device_id, asset_id=asset_id,
            start_time=time(7, 25), end_time=time(7, 36),
            start_date=datetime(2026, 4, 2, 7, 0, tzinfo=timezone.utc),
            end_date=datetime(2026, 4, 2, 23, 59, 59, tzinfo=timezone.utc),
        )

        # Mock "now" to April 2 at 3 PM PDT (22:00 UTC)
        fake_now = datetime(2026, 4, 2, 22, 0, 0, tzinfo=timezone.utc)
        with patch("cms.ui.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            resp = await client.get("/schedules")

        assert resp.status_code == 200
        html = resp.text
        assert "Expired Schedules" in html
        assert "Morning Flash" in html

    async def test_same_day_not_expired_if_time_window_ahead(self, client, db_session):
        """Schedule ending today whose time window hasn't started yet stays active."""
        from unittest.mock import patch
        device_id, asset_id = await self._seed(db_session)
        await set_setting(db_session, SETTING_TIMEZONE, "America/Los_Angeles")

        await self._create_schedule(
            db_session, name="Evening Show", device_id=device_id, asset_id=asset_id,
            start_time=time(18, 0), end_time=time(20, 0),
            start_date=datetime(2026, 4, 2, 7, 0, tzinfo=timezone.utc),
            end_date=datetime(2026, 4, 2, 23, 59, 59, tzinfo=timezone.utc),
        )

        # Mock "now" to April 2 at 3 PM PDT — evening window hasn't started
        fake_now = datetime(2026, 4, 2, 22, 0, 0, tzinfo=timezone.utc)
        with patch("cms.ui.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            resp = await client.get("/schedules")

        assert resp.status_code == 200
        html = resp.text
        assert "Evening Show" in html
        assert "Expired Schedules" not in html
