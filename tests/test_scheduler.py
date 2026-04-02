"""Tests for schedule evaluation and sync logic."""

import uuid
from datetime import datetime, time, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from cms.models.asset import Asset, AssetType
from cms.models.device import Device, DeviceGroup, DeviceStatus
from cms.models.schedule import Schedule
from cms.models.setting import CMSSetting
from cms.services.scheduler import (
    _matches_now,
    _schedule_to_entry,
    _times_overlap,
    _days_overlap,
    _dates_overlap,
    schedules_conflict,
    build_device_sync,
)


# ── Helpers ──


def _make_schedule(
    start_time: time,
    end_time: time,
    enabled: bool = True,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
    days_of_week: list[int] | None = None,
    priority: int = 0,
) -> Schedule:
    """Create a Schedule object for testing _matches_now (no DB needed)."""
    return Schedule(
        name="test",
        asset_id=uuid.uuid4(),
        enabled=enabled,
        start_time=start_time,
        end_time=end_time,
        start_date=start_date,
        end_date=end_date,
        days_of_week=days_of_week,
        priority=priority,
    )


# ── _matches_now tests ──


class TestMatchesNow:
    """Test the _matches_now schedule matching function."""

    def test_basic_match_within_window(self):
        s = _make_schedule(time(9, 0), time(17, 0))
        now = datetime(2026, 3, 28, 12, 0)  # noon
        assert _matches_now(s, now) is True

    def test_basic_no_match_before_window(self):
        s = _make_schedule(time(9, 0), time(17, 0))
        now = datetime(2026, 3, 28, 8, 59)
        assert _matches_now(s, now) is False

    def test_basic_no_match_after_window(self):
        s = _make_schedule(time(9, 0), time(17, 0))
        now = datetime(2026, 3, 28, 17, 30)
        assert _matches_now(s, now) is False

    def test_start_time_inclusive(self):
        """Start time is inclusive: exactly 9:00 should match."""
        s = _make_schedule(time(9, 0), time(17, 0))
        now = datetime(2026, 3, 28, 9, 0)
        assert _matches_now(s, now) is True

    def test_end_time_exclusive(self):
        """End time is exclusive: exactly 17:00 should NOT match."""
        s = _make_schedule(time(9, 0), time(17, 0))
        now = datetime(2026, 3, 28, 17, 0)
        assert _matches_now(s, now) is False

    def test_one_minute_before_end(self):
        """16:59 should still match."""
        s = _make_schedule(time(9, 0), time(17, 0))
        now = datetime(2026, 3, 28, 16, 59)
        assert _matches_now(s, now) is True

    def test_overnight_span_before_midnight(self):
        """22:00-06:00 should match at 23:00."""
        s = _make_schedule(time(22, 0), time(6, 0))
        now = datetime(2026, 3, 28, 23, 0)
        assert _matches_now(s, now) is True

    def test_overnight_span_after_midnight(self):
        """22:00-06:00 should match at 02:00."""
        s = _make_schedule(time(22, 0), time(6, 0))
        now = datetime(2026, 3, 29, 2, 0)
        assert _matches_now(s, now) is True

    def test_overnight_span_at_start(self):
        """22:00-06:00 should match at exactly 22:00."""
        s = _make_schedule(time(22, 0), time(6, 0))
        now = datetime(2026, 3, 28, 22, 0)
        assert _matches_now(s, now) is True

    def test_overnight_span_end_exclusive(self):
        """22:00-06:00 should NOT match at exactly 06:00."""
        s = _make_schedule(time(22, 0), time(6, 0))
        now = datetime(2026, 3, 29, 6, 0)
        assert _matches_now(s, now) is False

    def test_overnight_span_no_match_afternoon(self):
        """22:00-06:00 should NOT match at 14:00."""
        s = _make_schedule(time(22, 0), time(6, 0))
        now = datetime(2026, 3, 28, 14, 0)
        assert _matches_now(s, now) is False

    def test_disabled_schedule(self):
        s = _make_schedule(time(0, 0), time(23, 59), enabled=False)
        now = datetime(2026, 3, 28, 12, 0)
        assert _matches_now(s, now) is False

    def test_before_start_date(self):
        s = _make_schedule(
            time(9, 0), time(17, 0),
            start_date=datetime(2026, 4, 1, tzinfo=timezone.utc),
        )
        now = datetime(2026, 3, 28, 12, 0, tzinfo=timezone.utc)
        assert _matches_now(s, now) is False

    def test_after_end_date(self):
        s = _make_schedule(
            time(9, 0), time(17, 0),
            end_date=datetime(2026, 3, 27, tzinfo=timezone.utc),
        )
        now = datetime(2026, 3, 28, 12, 0, tzinfo=timezone.utc)
        assert _matches_now(s, now) is False

    def test_within_date_range(self):
        s = _make_schedule(
            time(9, 0), time(17, 0),
            start_date=datetime(2026, 3, 1, tzinfo=timezone.utc),
            end_date=datetime(2026, 3, 31, tzinfo=timezone.utc),
        )
        now = datetime(2026, 3, 28, 12, 0, tzinfo=timezone.utc)
        assert _matches_now(s, now) is True

    def test_day_of_week_match(self):
        """March 28, 2026 is a Saturday (isoweekday=6)."""
        s = _make_schedule(time(9, 0), time(17, 0), days_of_week=[6, 7])
        now = datetime(2026, 3, 28, 12, 0)
        assert _matches_now(s, now) is True

    def test_day_of_week_no_match(self):
        """March 28, 2026 is a Saturday — weekday-only schedule should not match."""
        s = _make_schedule(time(9, 0), time(17, 0), days_of_week=[1, 2, 3, 4, 5])
        now = datetime(2026, 3, 28, 12, 0)
        assert _matches_now(s, now) is False

    def test_no_days_of_week_means_every_day(self):
        s = _make_schedule(time(9, 0), time(17, 0), days_of_week=None)
        now = datetime(2026, 3, 28, 12, 0)
        assert _matches_now(s, now) is True

    def test_one_shot_matches_on_date(self):
        """One-shot schedule (same start and end date) matches on that date."""
        s = _make_schedule(
            time(8, 0), time(8, 30),
            start_date=datetime(2026, 4, 1),
            end_date=datetime(2026, 4, 1),
        )
        now = datetime(2026, 4, 1, 8, 15)
        assert _matches_now(s, now) is True

    def test_one_shot_no_match_different_date(self):
        """One-shot schedule does not match on a different date."""
        s = _make_schedule(
            time(8, 0), time(8, 30),
            start_date=datetime(2026, 4, 1),
            end_date=datetime(2026, 4, 1),
        )
        now = datetime(2026, 4, 2, 8, 15)
        assert _matches_now(s, now) is False

    def test_one_shot_no_match_before_date(self):
        """One-shot schedule does not match before its date."""
        s = _make_schedule(
            time(8, 0), time(8, 30),
            start_date=datetime(2026, 4, 1),
            end_date=datetime(2026, 4, 1),
        )
        now = datetime(2026, 3, 31, 8, 15)
        assert _matches_now(s, now) is False

    def test_one_minute_window(self):
        """13:20-13:21 should only match at 13:20."""
        s = _make_schedule(time(13, 20), time(13, 21))
        assert _matches_now(s, datetime(2026, 3, 28, 13, 19)) is False
        assert _matches_now(s, datetime(2026, 3, 28, 13, 20)) is True
        assert _matches_now(s, datetime(2026, 3, 28, 13, 21)) is False

    def test_midnight_exactly(self):
        """Schedule spanning midnight: 23:00-01:00, check at 00:00."""
        s = _make_schedule(time(23, 0), time(1, 0))
        now = datetime(2026, 3, 29, 0, 0)
        assert _matches_now(s, now) is True

    def test_same_start_end_time(self):
        """Start == end (0-length window) should not match anything."""
        s = _make_schedule(time(12, 0), time(12, 0))
        assert _matches_now(s, datetime(2026, 3, 28, 12, 0)) is False
        assert _matches_now(s, datetime(2026, 3, 28, 11, 59)) is False

    def test_full_day_schedule(self):
        """00:00-00:00 (overnight path: start <= end is False since equal).
        This is treated as same-time, so nothing matches."""
        s = _make_schedule(time(0, 0), time(0, 0))
        assert _matches_now(s, datetime(2026, 3, 28, 12, 0)) is False

    def test_nearly_full_day(self):
        """00:00-23:59 should match all day except 23:59."""
        s = _make_schedule(time(0, 0), time(23, 59))
        assert _matches_now(s, datetime(2026, 3, 28, 0, 0)) is True
        assert _matches_now(s, datetime(2026, 3, 28, 12, 0)) is True
        assert _matches_now(s, datetime(2026, 3, 28, 23, 58)) is True
        assert _matches_now(s, datetime(2026, 3, 28, 23, 59)) is False


# ── _schedule_to_entry tests ──


class TestScheduleToEntry:
    def test_basic_conversion(self):
        asset = Asset(
            filename="video.mp4",
            asset_type=AssetType.VIDEO,
            size_bytes=1000,
            checksum="abc",
        )

        s = Schedule(
            name="Test Schedule",
            asset_id=uuid.uuid4(),
            start_time=time(9, 0),
            end_time=time(17, 0),
            days_of_week=[1, 2, 3],
            priority=5,
        )
        s.id = uuid.UUID("12345678-1234-1234-1234-123456789abc")
        s.asset = asset

        entry = _schedule_to_entry(s)
        assert entry.id == "12345678-1234-1234-1234-123456789abc"
        assert entry.name == "Test Schedule"
        assert entry.asset == "video.mp4"
        assert entry.start_time == "09:00"
        assert entry.end_time == "17:00"
        assert entry.start_date is None
        assert entry.end_date is None
        assert entry.days_of_week == [1, 2, 3]
        assert entry.priority == 5

    def test_with_date_range(self):
        asset = Asset(
            filename="promo.mp4",
            asset_type=AssetType.VIDEO,
            size_bytes=1000,
            checksum="def",
        )

        s = Schedule(
            name="Dated",
            asset_id=uuid.uuid4(),
            start_time=time(8, 0),
            end_time=time(12, 0),
            start_date=datetime(2026, 4, 1, tzinfo=timezone.utc),
            end_date=datetime(2026, 4, 30, tzinfo=timezone.utc),
            priority=0,
        )
        s.asset = asset

        entry = _schedule_to_entry(s)
        assert entry.start_date == "2026-04-01"
        assert entry.end_date == "2026-04-30"


# ── build_device_sync tests (require DB) ──


@pytest.mark.asyncio
class TestBuildDeviceSync:
    @pytest_asyncio.fixture
    async def db(self, db_engine):
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            yield session

    async def _setup_device(self, db, group=None, default_asset=None):
        """Create a device, optionally with group and default asset."""
        device = Device(
            id="sync-pi-01",
            name="Sync Test",
            status=DeviceStatus.ADOPTED,
        )
        if group:
            device.group = group
        if default_asset:
            device.default_asset = default_asset
        db.add(device)
        await db.commit()
        return device

    async def _setup_tz(self, db, tz="America/Los_Angeles"):
        setting = CMSSetting(key="timezone", value=tz)
        db.add(setting)
        await db.commit()

    async def test_empty_schedule(self, db):
        """Device with no schedules gets empty schedule list."""
        await self._setup_tz(db)
        await self._setup_device(db)

        sync = await build_device_sync("sync-pi-01", db)
        assert sync is not None
        assert sync.schedules == []
        assert sync.timezone == "America/Los_Angeles"

    async def test_with_schedule(self, db):
        """Device gets its assigned schedule."""
        await self._setup_tz(db)
        asset = Asset(filename="video.mp4", asset_type=AssetType.VIDEO, size_bytes=1000, checksum="abc")
        db.add(asset)
        await db.flush()

        device = await self._setup_device(db)

        sched = Schedule(
            name="Test",
            device_id="sync-pi-01",
            asset_id=asset.id,
            start_time=time(9, 0),
            end_time=time(17, 0),
        )
        db.add(sched)
        await db.commit()

        sync = await build_device_sync("sync-pi-01", db)
        assert len(sync.schedules) == 1
        assert sync.schedules[0].asset == "video.mp4"
        assert sync.schedules[0].name == "Test"

    async def test_group_schedule(self, db):
        """Device in a group gets group-targeted schedule."""
        await self._setup_tz(db)
        asset = Asset(filename="group-vid.mp4", asset_type=AssetType.VIDEO, size_bytes=500, checksum="def")
        group = DeviceGroup(name="Lobby")
        db.add_all([asset, group])
        await db.flush()

        device = await self._setup_device(db, group=group)

        sched = Schedule(
            name="Group Sched",
            group_id=group.id,
            asset_id=asset.id,
            start_time=time(8, 0),
            end_time=time(20, 0),
        )
        db.add(sched)
        await db.commit()

        sync = await build_device_sync("sync-pi-01", db)
        assert len(sync.schedules) == 1
        assert sync.schedules[0].name == "Group Sched"

    async def test_default_asset_device_level(self, db):
        """Device-level default asset appears in sync."""
        await self._setup_tz(db)
        default = Asset(filename="splash.png", asset_type=AssetType.IMAGE, size_bytes=100, checksum="spl")
        db.add(default)
        await db.flush()

        await self._setup_device(db, default_asset=default)

        sync = await build_device_sync("sync-pi-01", db)
        assert sync.default_asset == "splash.png"

    async def test_default_asset_group_fallback(self, db):
        """Group default asset is used when device has none."""
        await self._setup_tz(db)
        group_default = Asset(filename="group-splash.png", asset_type=AssetType.IMAGE, size_bytes=100, checksum="gs")
        group = DeviceGroup(name="Stores")
        db.add_all([group_default, group])
        await db.flush()
        group.default_asset_id = group_default.id
        await db.flush()

        await self._setup_device(db, group=group)

        sync = await build_device_sync("sync-pi-01", db)
        assert sync.default_asset == "group-splash.png"

    async def test_nonexistent_device_returns_none(self, db):
        await self._setup_tz(db)
        sync = await build_device_sync("no-such-device", db)
        assert sync is None

    async def test_expired_schedule_excluded(self, db):
        """Schedule with end_date in the past is excluded."""
        await self._setup_tz(db)
        asset = Asset(filename="old.mp4", asset_type=AssetType.VIDEO, size_bytes=100, checksum="old")
        db.add(asset)
        await db.flush()

        await self._setup_device(db)

        sched = Schedule(
            name="Expired",
            device_id="sync-pi-01",
            asset_id=asset.id,
            start_time=time(9, 0),
            end_time=time(17, 0),
            end_date=datetime(2025, 1, 1, tzinfo=timezone.utc),
        )
        db.add(sched)
        await db.commit()

        sync = await build_device_sync("sync-pi-01", db)
        assert sync.schedules == []

    async def test_naive_datetime_does_not_crash(self, db):
        """Naive end_date (as returned by aiosqlite) must not crash build_device_sync."""
        await self._setup_tz(db)
        asset = Asset(filename="naive.mp4", asset_type=AssetType.VIDEO, size_bytes=100, checksum="nv")
        db.add(asset)
        await db.flush()

        await self._setup_device(db)

        # Naive datetime — no tzinfo, mimics what aiosqlite returns
        sched = Schedule(
            name="Naive Dates",
            device_id="sync-pi-01",
            asset_id=asset.id,
            start_time=time(9, 0),
            end_time=time(17, 0),
            start_date=datetime(2025, 1, 1),
            end_date=datetime(2099, 12, 31),
        )
        db.add(sched)
        await db.commit()

        sync = await build_device_sync("sync-pi-01", db)
        assert len(sync.schedules) == 1
        assert sync.schedules[0].name == "Naive Dates"

    async def test_end_date_uses_local_timezone_not_utc(self, db):
        """Schedule ending today in local TZ must not be filtered out when UTC date is tomorrow.

        Scenario: CMS timezone is America/Los_Angeles (UTC-7).
        It's 9 PM PDT April 1st (= 4 AM UTC April 2nd).
        Schedule has end_date=April 1st.
        The schedule should still be included because it's still April 1st locally.
        """
        from unittest.mock import patch
        await self._setup_tz(db, tz="America/Los_Angeles")
        asset = Asset(filename="tz-test.mp4", asset_type=AssetType.VIDEO, size_bytes=100, checksum="tz")
        db.add(asset)
        await db.flush()

        await self._setup_device(db)

        sched = Schedule(
            name="TZ End Date",
            device_id="sync-pi-01",
            asset_id=asset.id,
            start_time=time(20, 0),
            end_time=time(22, 0),
            start_date=datetime(2026, 4, 1, tzinfo=timezone.utc),
            end_date=datetime(2026, 4, 1, tzinfo=timezone.utc),
        )
        db.add(sched)
        await db.commit()

        # 4 AM UTC April 2nd = 9 PM PDT April 1st
        fake_now = datetime(2026, 4, 2, 4, 0, 0, tzinfo=timezone.utc)
        with patch("cms.services.scheduler.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            mock_dt.combine = datetime.combine
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            sync = await build_device_sync("sync-pi-01", db)

        assert len(sync.schedules) == 1
        assert sync.schedules[0].name == "TZ End Date"

    async def test_disabled_schedule_excluded(self, db):
        """Disabled schedule is excluded from sync."""
        await self._setup_tz(db)
        asset = Asset(filename="dis.mp4", asset_type=AssetType.VIDEO, size_bytes=100, checksum="dis")
        db.add(asset)
        await db.flush()

        await self._setup_device(db)

        sched = Schedule(
            name="Disabled",
            device_id="sync-pi-01",
            asset_id=asset.id,
            start_time=time(9, 0),
            end_time=time(17, 0),
            enabled=False,
        )
        db.add(sched)
        await db.commit()

        sync = await build_device_sync("sync-pi-01", db)
        assert sync.schedules == []

    async def test_other_device_schedule_excluded(self, db):
        """Schedule for a different device is not included."""
        await self._setup_tz(db)
        asset = Asset(filename="other.mp4", asset_type=AssetType.VIDEO, size_bytes=100, checksum="oth")
        other_device = Device(id="other-pi", name="Other", status=DeviceStatus.ADOPTED)
        db.add_all([asset, other_device])
        await db.flush()

        await self._setup_device(db)

        sched = Schedule(
            name="Not Mine",
            device_id="other-pi",
            asset_id=asset.id,
            start_time=time(9, 0),
            end_time=time(17, 0),
        )
        db.add(sched)
        await db.commit()

        sync = await build_device_sync("sync-pi-01", db)
        assert sync.schedules == []


# ── Overlap helper tests ──


class TestTimesOverlap:
    def test_no_overlap(self):
        assert _times_overlap(time(9, 0), time(12, 0), time(13, 0), time(17, 0)) is False

    def test_adjacent_no_overlap(self):
        """End of one == start of next — no overlap (end is exclusive)."""
        assert _times_overlap(time(9, 0), time(12, 0), time(12, 0), time(17, 0)) is False

    def test_overlap(self):
        assert _times_overlap(time(9, 0), time(14, 0), time(12, 0), time(17, 0)) is True

    def test_contained(self):
        assert _times_overlap(time(9, 0), time(17, 0), time(10, 0), time(12, 0)) is True

    def test_identical(self):
        assert _times_overlap(time(9, 0), time(17, 0), time(9, 0), time(17, 0)) is True

    def test_overnight_vs_daytime(self):
        """22:00-06:00 overlaps with 05:00-08:00."""
        assert _times_overlap(time(22, 0), time(6, 0), time(5, 0), time(8, 0)) is True

    def test_overnight_vs_daytime_no_overlap(self):
        """22:00-06:00 does not overlap 08:00-12:00."""
        assert _times_overlap(time(22, 0), time(6, 0), time(8, 0), time(12, 0)) is False

    def test_both_overnight(self):
        """Two overnight spans always overlap."""
        assert _times_overlap(time(22, 0), time(4, 0), time(23, 0), time(6, 0)) is True

    def test_zero_length_no_overlap(self):
        assert _times_overlap(time(9, 0), time(9, 0), time(8, 0), time(10, 0)) is False


class TestDaysOverlap:
    def test_none_means_every_day(self):
        assert _days_overlap(None, [1, 2, 3]) is True
        assert _days_overlap([1, 2], None) is True
        assert _days_overlap(None, None) is True

    def test_shared_day(self):
        assert _days_overlap([1, 2, 3], [3, 4, 5]) is True

    def test_no_shared_day(self):
        assert _days_overlap([1, 2, 3], [4, 5, 6]) is False


class TestDatesOverlap:
    def test_both_unbounded(self):
        assert _dates_overlap(None, None, None, None) is True

    def test_one_ends_before_other_starts(self):
        assert _dates_overlap(
            datetime(2026, 1, 1), datetime(2026, 1, 31),
            datetime(2026, 3, 1), datetime(2026, 3, 31),
        ) is False

    def test_overlapping_ranges(self):
        assert _dates_overlap(
            datetime(2026, 1, 1), datetime(2026, 3, 15),
            datetime(2026, 3, 1), datetime(2026, 3, 31),
        ) is True

    def test_same_day(self):
        assert _dates_overlap(
            datetime(2026, 4, 1), datetime(2026, 4, 1),
            datetime(2026, 4, 1), datetime(2026, 4, 1),
        ) is True

    def test_one_unbounded_end(self):
        assert _dates_overlap(
            datetime(2026, 1, 1), None,
            datetime(2026, 6, 1), datetime(2026, 6, 30),
        ) is True


class TestSchedulesConflict:
    def _make(self, device_id="dev-1", group_id=None, priority=0,
              start_time=time(9, 0), end_time=time(17, 0),
              start_date=None, end_date=None, days_of_week=None):
        return Schedule(
            name="test",
            asset_id=uuid.uuid4(),
            device_id=device_id,
            group_id=group_id,
            enabled=True,
            start_time=start_time,
            end_time=end_time,
            start_date=start_date,
            end_date=end_date,
            days_of_week=days_of_week,
            priority=priority,
        )

    def test_conflict_same_target_same_priority(self):
        a = self._make()
        b = self._make()
        assert schedules_conflict(a, b) is True

    def test_no_conflict_different_priority(self):
        a = self._make(priority=0)
        b = self._make(priority=1)
        assert schedules_conflict(a, b) is False

    def test_no_conflict_different_device(self):
        a = self._make(device_id="dev-1")
        b = self._make(device_id="dev-2")
        assert schedules_conflict(a, b) is False

    def test_no_conflict_different_times(self):
        a = self._make(start_time=time(9, 0), end_time=time(12, 0))
        b = self._make(start_time=time(14, 0), end_time=time(17, 0))
        assert schedules_conflict(a, b) is False

    def test_no_conflict_different_days(self):
        a = self._make(days_of_week=[1, 2, 3])
        b = self._make(days_of_week=[4, 5, 6])
        assert schedules_conflict(a, b) is False

    def test_no_conflict_different_dates(self):
        a = self._make(start_date=datetime(2026, 1, 1), end_date=datetime(2026, 1, 31))
        b = self._make(start_date=datetime(2026, 3, 1), end_date=datetime(2026, 3, 31))
        assert schedules_conflict(a, b) is False

    def test_conflict_overlapping_everything(self):
        a = self._make(
            start_time=time(8, 0), end_time=time(12, 0),
            days_of_week=[1, 2, 3],
            start_date=datetime(2026, 4, 1), end_date=datetime(2026, 4, 30),
        )
        b = self._make(
            start_time=time(10, 0), end_time=time(14, 0),
            days_of_week=[3, 4, 5],
            start_date=datetime(2026, 4, 15), end_date=datetime(2026, 5, 15),
        )
        assert schedules_conflict(a, b) is True
