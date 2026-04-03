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
        """end_date stored as midnight UTC should NOT be falsely expired
        due to timezone conversion for users behind UTC.

        Scenario: CMS timezone is America/Los_Angeles (UTC-7).
        end_date is stored as 2026-04-02 00:00:00+00 (midnight UTC).
        The calendar date the user picked was April 2.
        On April 2 local (with time window still active), this schedule
        should be in Active Schedules, not Expired.
        """
        from unittest.mock import patch
        device_id, asset_id = await self._seed(db_session)
        await set_setting(db_session, SETTING_TIMEZONE, "America/Los_Angeles")

        # end_date at UTC midnight — calendar date is April 2 (what the user picked)
        await self._create_schedule(
            db_session, name="UTC Midnight Trap", device_id=device_id, asset_id=asset_id,
            start_date=datetime(2026, 4, 1, 0, 0, tzinfo=timezone.utc),
            end_date=datetime(2026, 4, 2, 0, 0, tzinfo=timezone.utc),
        )

        # Mock "now" to April 2 at noon PDT (19:00 UTC) — time window 9-17 still open
        fake_now = datetime(2026, 4, 2, 19, 0, 0, tzinfo=timezone.utc)
        with patch("cms.ui.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            resp = await client.get("/schedules")

        assert resp.status_code == 200
        html = resp.text
        # April 2 calendar date should NOT be expired on April 2 local
        assert "UTC Midnight Trap" in html
        assert "Expired Schedules" not in html

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

    async def test_edit_expired_to_future_moves_to_active(self, client, db_session):
        """Editing an expired schedule to have a future end_date should
        move it from Expired to Active on the schedules page.

        Regression: user edited an expired schedule's dates to the future;
        the dashboard correctly showed it as 'upcoming' but the schedules
        page still listed it under Expired.
        """
        device_id, asset_id = await self._seed(db_session)

        # Create an expired schedule via the API
        yesterday = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%dT00:00:00Z")
        week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT00:00:00Z")
        create_resp = await client.post("/api/schedules", json={
            "name": "Revivedable",
            "device_id": device_id,
            "asset_id": str(asset_id),
            "start_time": "09:00",
            "end_time": "17:00",
            "start_date": week_ago,
            "end_date": yesterday,
        })
        assert create_resp.status_code == 201
        sched_id = create_resp.json()["id"]

        # Confirm it's in Expired
        page = await client.get("/schedules")
        assert "Expired Schedules" in page.text
        assert "Revivedable" in page.text

        # Update to future dates (like the edit modal would send)
        tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
        next_week = (datetime.now(timezone.utc) + timedelta(days=7)).strftime("%Y-%m-%d")
        patch_resp = await client.patch(f"/api/schedules/{sched_id}", json={
            "start_date": tomorrow,
            "end_date": next_week,
        })
        assert patch_resp.status_code == 200

        # Reload the schedules page — schedule should be in Active, not Expired
        page2 = await client.get("/schedules")
        html = page2.text
        assert "Revivedable" in html
        # It should NOT be in the Expired section
        if "Expired Schedules" in html:
            # Parse to check which table contains the schedule
            expired_pos = html.index("Expired Schedules")
            active_pos = html.index("Active Schedules")
            name_pos = html.index("Revivedable")
            assert name_pos < expired_pos, (
                "Schedule still appears in Expired section after updating to future dates"
            )

    async def test_end_date_today_not_falsely_expired_behind_utc(self, client, db_session):
        """A schedule with end_date = today (midnight UTC) must NOT be marked
        expired for timezones behind UTC.

        Bug: dates from the date picker are stored as midnight UTC.
        The old code converted ``end_date`` to local time before comparing,
        causing midnight UTC to shift to the previous local day for negative-
        offset timezones.  E.g. ``2026-04-02T00:00Z`` → April 1 in PDT →
        falsely expired on April 2.

        The fix: compare ``end_date.date()`` (the calendar date the user
        picked) directly against ``today_local``, matching how
        ``_matches_now`` and ``get_upcoming_schedules`` already work.
        """
        from unittest.mock import patch as mock_patch
        device_id, asset_id = await self._seed(db_session)
        await set_setting(db_session, SETTING_TIMEZONE, "America/Los_Angeles")

        # Simulate date picker sending "2026-04-02" → stored as midnight UTC
        await self._create_schedule(
            db_session, name="Today Picker", device_id=device_id, asset_id=asset_id,
            start_date=datetime(2026, 4, 1, 0, 0, tzinfo=timezone.utc),
            end_date=datetime(2026, 4, 2, 0, 0, tzinfo=timezone.utc),
        )

        # Mock "now" to April 2 at 10 AM PDT (17:00 UTC)
        fake_now = datetime(2026, 4, 2, 17, 0, 0, tzinfo=timezone.utc)
        with mock_patch("cms.ui.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            resp = await client.get("/schedules")

        assert resp.status_code == 200
        html = resp.text
        # The schedule should be in Active, not Expired
        assert "Today Picker" in html
        assert "Expired Schedules" not in html, (
            "Schedule with end_date=today falsely categorized as expired "
            "due to UTC→local timezone shift"
        )
