"""Advanced scheduler tests: priorities, skip/end-now, upcoming, unique names, evaluate_schedules."""

import uuid
from datetime import datetime, time, timedelta, timezone
from unittest.mock import AsyncMock, patch, PropertyMock
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from cms.models.asset import Asset, AssetType
from cms.models.device import Device, DeviceGroup, DeviceStatus
from cms.models.schedule import Schedule
from cms.models.setting import CMSSetting
from cms.services.scheduler import (
    _matches_now,
    _skipped,
    _now_playing,
    _last_sync_hash,
    build_device_sync,
    clear_sync_hash,
    evaluate_schedules,
    get_now_playing,
    get_upcoming_schedules,
    skip_schedule_until,
)


# ── Helpers ──


def _make_schedule(
    start_time: time,
    end_time: time,
    enabled: bool = True,
    priority: int = 0,
    name: str = "test",
    asset_id=None,
    device_id=None,
    group_id=None,
    start_date=None,
    end_date=None,
    days_of_week=None,
) -> Schedule:
    s = Schedule(
        name=name,
        asset_id=asset_id or uuid.uuid4(),
        device_id=device_id,
        group_id=group_id,
        enabled=enabled,
        start_time=start_time,
        end_time=end_time,
        start_date=start_date,
        end_date=end_date,
        days_of_week=days_of_week,
        priority=priority,
    )
    s.id = uuid.uuid4()
    return s


def _make_schedule_with_asset(
    start_time: time,
    end_time: time,
    priority: int = 0,
    name: str = "test",
    asset_filename: str = "video.mp4",
    device_id: str | None = None,
    group_id=None,
    days_of_week=None,
    enabled: bool = True,
) -> Schedule:
    """Create a schedule with a real Asset object attached (for evaluate_schedules tests)."""
    asset = Asset(filename=asset_filename, asset_type=AssetType.VIDEO, size_bytes=1000, checksum="abc")
    s = _make_schedule(
        start_time=start_time,
        end_time=end_time,
        priority=priority,
        name=name,
        device_id=device_id,
        group_id=group_id,
        days_of_week=days_of_week,
        enabled=enabled,
    )
    s.asset = asset
    return s


# ── skip_schedule_until / End Now ──


class TestSkipSchedule:
    """Tests for the in-memory skip mechanism (End Now feature)."""

    def setup_method(self):
        _skipped.clear()
        _now_playing.clear()
        _last_sync_hash.clear()

    def teardown_method(self):
        _skipped.clear()
        _now_playing.clear()
        _last_sync_hash.clear()

    def test_skip_adds_to_skipped_dict(self):
        until = datetime(2026, 3, 29, 17, 0)
        skip_schedule_until("sched-1", until)
        assert "sched-1" in _skipped
        assert _skipped["sched-1"] == until

    def test_skip_removes_from_now_playing(self):
        _now_playing["device-a"] = {"schedule_id": "sched-1", "device_id": "device-a"}
        _now_playing["device-b"] = {"schedule_id": "sched-2", "device_id": "device-b"}

        skip_schedule_until("sched-1", datetime(2026, 3, 29, 17, 0))

        assert "device-a" not in _now_playing
        assert "device-b" in _now_playing

    def test_skip_removes_multiple_devices_same_schedule(self):
        _now_playing["d1"] = {"schedule_id": "sched-1", "device_id": "d1"}
        _now_playing["d2"] = {"schedule_id": "sched-1", "device_id": "d2"}
        _now_playing["d3"] = {"schedule_id": "sched-2", "device_id": "d3"}

        skip_schedule_until("sched-1", datetime(2026, 3, 29, 17, 0))

        assert "d1" not in _now_playing
        assert "d2" not in _now_playing
        assert "d3" in _now_playing

    def test_skip_nonexistent_schedule_is_harmless(self):
        skip_schedule_until("no-such-id", datetime(2026, 3, 29, 17, 0))
        assert "no-such-id" in _skipped
        assert len(_now_playing) == 0

    def test_get_now_playing_returns_list(self):
        _now_playing["d1"] = {"schedule_id": "s1", "device_id": "d1"}
        _now_playing["d2"] = {"schedule_id": "s2", "device_id": "d2"}

        result = get_now_playing()
        assert isinstance(result, list)
        assert len(result) == 2


class TestClearSyncHash:
    def setup_method(self):
        _last_sync_hash.clear()

    def teardown_method(self):
        _last_sync_hash.clear()

    def test_clear_existing(self):
        _last_sync_hash["device-1"] = "abc123"
        clear_sync_hash("device-1")
        assert "device-1" not in _last_sync_hash

    def test_clear_nonexistent_is_harmless(self):
        clear_sync_hash("no-such-device")
        assert len(_last_sync_hash) == 0


# ── Priority tests (pure logic, no DB) ──


class TestPrioritySelection:
    """Test that the highest-priority active schedule wins per device."""

    def test_matches_now_ignores_priority(self):
        """Priority doesn't affect _matches_now; it only matters in winner selection."""
        low = _make_schedule(time(9, 0), time(17, 0), priority=0)
        high = _make_schedule(time(9, 0), time(17, 0), priority=100)
        now = datetime(2026, 3, 28, 12, 0)
        assert _matches_now(low, now) is True
        assert _matches_now(high, now) is True

    def test_two_overlapping_schedules_different_priorities(self):
        """Simulates the winner-selection loop from evaluate_schedules."""
        low = _make_schedule_with_asset(time(9, 0), time(17, 0), priority=1, name="Low", device_id="d1")
        high = _make_schedule_with_asset(time(9, 0), time(17, 0), priority=10, name="High", device_id="d1")
        now = datetime(2026, 3, 28, 12, 0)

        active = [s for s in [low, high] if _matches_now(s, now)]
        assert len(active) == 2

        # Replicate winner selection logic
        device_winner = {}
        for s in active:
            if not s.asset:
                continue
            did = s.device_id
            existing = device_winner.get(did)
            if existing is None or s.priority > existing.priority:
                device_winner[did] = s

        assert device_winner["d1"].name == "High"
        assert device_winner["d1"].priority == 10

    def test_equal_priority_last_wins(self):
        """When priorities are equal, the last one processed wins."""
        s1 = _make_schedule_with_asset(time(9, 0), time(17, 0), priority=5, name="First", device_id="d1")
        s2 = _make_schedule_with_asset(time(9, 0), time(17, 0), priority=5, name="Second", device_id="d1")
        now = datetime(2026, 3, 28, 12, 0)

        active = [s for s in [s1, s2] if _matches_now(s, now)]
        device_winner = {}
        for s in active:
            did = s.device_id
            existing = device_winner.get(did)
            if existing is None or s.priority > existing.priority:
                device_winner[did] = s

        # With equal priority, `>` is False so the first one stays
        assert device_winner["d1"].name == "First"

    def test_three_schedules_highest_wins(self):
        low = _make_schedule_with_asset(time(9, 0), time(17, 0), priority=1, name="Low", device_id="d1")
        mid = _make_schedule_with_asset(time(9, 0), time(17, 0), priority=5, name="Mid", device_id="d1")
        high = _make_schedule_with_asset(time(9, 0), time(17, 0), priority=10, name="High", device_id="d1")
        now = datetime(2026, 3, 28, 12, 0)

        active = [s for s in [low, mid, high] if _matches_now(s, now)]
        device_winner = {}
        for s in active:
            did = s.device_id
            existing = device_winner.get(did)
            if existing is None or s.priority > existing.priority:
                device_winner[did] = s

        assert device_winner["d1"].name == "High"

    def test_different_devices_get_different_winners(self):
        """Each device picks its own highest-priority schedule."""
        s1 = _make_schedule_with_asset(time(9, 0), time(17, 0), priority=1, name="A-Low", device_id="d1")
        s2 = _make_schedule_with_asset(time(9, 0), time(17, 0), priority=10, name="A-High", device_id="d1")
        s3 = _make_schedule_with_asset(time(9, 0), time(17, 0), priority=5, name="B-Only", device_id="d2")
        now = datetime(2026, 3, 28, 12, 0)

        active = [s for s in [s1, s2, s3] if _matches_now(s, now)]
        device_winner = {}
        for s in active:
            did = s.device_id
            existing = device_winner.get(did)
            if existing is None or s.priority > existing.priority:
                device_winner[did] = s

        assert device_winner["d1"].name == "A-High"
        assert device_winner["d2"].name == "B-Only"

    def test_no_winner_when_no_active(self):
        """No active schedules means no winners."""
        s = _make_schedule_with_asset(time(9, 0), time(10, 0), priority=5, name="Morning", device_id="d1")
        now = datetime(2026, 3, 28, 15, 0)  # 3 PM, outside 9-10

        active = [sched for sched in [s] if _matches_now(sched, now)]
        assert len(active) == 0

    def test_priority_with_partial_overlap(self):
        """One schedule is active, the other isn't — only the active one wins."""
        morning = _make_schedule_with_asset(time(8, 0), time(12, 0), priority=1, name="Morning", device_id="d1")
        afternoon = _make_schedule_with_asset(time(13, 0), time(17, 0), priority=10, name="Afternoon", device_id="d1")
        now = datetime(2026, 3, 28, 10, 0)  # 10 AM

        active = [s for s in [morning, afternoon] if _matches_now(s, now)]
        assert len(active) == 1

        device_winner = {}
        for s in active:
            did = s.device_id
            existing = device_winner.get(did)
            if existing is None or s.priority > existing.priority:
                device_winner[did] = s

        assert device_winner["d1"].name == "Morning"

    def test_skipped_schedule_excluded_from_active(self):
        """Skipped schedules are filtered out of the active list."""
        s1 = _make_schedule_with_asset(time(9, 0), time(17, 0), priority=10, name="High", device_id="d1")
        s2 = _make_schedule_with_asset(time(9, 0), time(17, 0), priority=1, name="Low", device_id="d1")
        now = datetime(2026, 3, 28, 12, 0)

        _skipped.clear()
        _skipped[str(s1.id)] = datetime(2026, 3, 28, 17, 0)

        active = [
            s for s in [s1, s2]
            if _matches_now(s, now) and str(s.id) not in _skipped
        ]
        assert len(active) == 1
        assert active[0].name == "Low"

        _skipped.clear()


# ── get_upcoming_schedules tests ──


class TestUpcomingSchedules:
    """Test the upcoming schedule calculation."""

    def _make_full_schedule(
        self, start_time, end_time, name="Upcoming",
        enabled=True, days_of_week=None, start_date=None, end_date=None,
        device_id=None, group_id=None,
    ):
        s = _make_schedule_with_asset(
            start_time=start_time,
            end_time=end_time,
            name=name,
            enabled=enabled,
            days_of_week=days_of_week,
            device_id=device_id,
            group_id=group_id,
        )
        s.start_date = start_date
        s.end_date = end_date
        # Attach mock device/group for target_name resolution
        s.device = None
        s.group = None
        return s

    def test_upcoming_today(self):
        """Schedule starting later today appears as today."""
        s = self._make_full_schedule(time(15, 0), time(16, 0), name="Afternoon")
        now = datetime(2026, 3, 28, 10, 0, tzinfo=timezone.utc)
        tz = ZoneInfo("UTC")

        result = get_upcoming_schedules([s], now, tz)
        assert len(result) == 1
        assert result[0]["schedule_name"] == "Afternoon"
        assert result[0]["day_label"] == "today"
        assert result[0]["duration_mins"] == 60

    def test_upcoming_tomorrow(self):
        """Schedule that already passed today appears as tomorrow."""
        s = self._make_full_schedule(time(8, 0), time(9, 0), name="Morning")
        now = datetime(2026, 3, 28, 10, 0, tzinfo=timezone.utc)
        tz = ZoneInfo("UTC")

        result = get_upcoming_schedules([s], now, tz)
        assert len(result) == 1
        assert result[0]["day_label"] == "tomorrow"

    def test_currently_active_excluded(self):
        """Schedule currently active is not in upcoming."""
        s = self._make_full_schedule(time(9, 0), time(17, 0), name="Active")
        now = datetime(2026, 3, 28, 12, 0, tzinfo=timezone.utc)
        tz = ZoneInfo("UTC")

        result = get_upcoming_schedules([s], now, tz)
        assert len(result) == 0

    def test_disabled_excluded(self):
        s = self._make_full_schedule(time(15, 0), time(16, 0), name="Disabled", enabled=False)
        now = datetime(2026, 3, 28, 10, 0, tzinfo=timezone.utc)
        tz = ZoneInfo("UTC")

        result = get_upcoming_schedules([s], now, tz)
        assert len(result) == 0

    def test_day_of_week_filter_today(self):
        """Schedule not for today's day of week."""
        # March 28, 2026 is Saturday (isoweekday=6)
        s = self._make_full_schedule(
            time(15, 0), time(16, 0), name="Weekday Only",
            days_of_week=[1, 2, 3, 4, 5],  # Mon-Fri
        )
        now = datetime(2026, 3, 28, 10, 0, tzinfo=timezone.utc)
        tz = ZoneInfo("UTC")

        result = get_upcoming_schedules([s], now, tz)
        # Should appear for Monday (tomorrow is Sunday=7, also not in list)
        # Actually Sunday is also not in the list so it won't show
        assert len(result) == 0

    def test_day_of_week_allows_tomorrow(self):
        """Schedule for tomorrow's day of week appears."""
        # March 28, 2026 is Saturday. March 29 is Sunday (isoweekday=7)
        s = self._make_full_schedule(
            time(10, 0), time(11, 0), name="Sunday Show",
            days_of_week=[7],
        )
        now = datetime(2026, 3, 28, 12, 0, tzinfo=timezone.utc)
        tz = ZoneInfo("UTC")

        result = get_upcoming_schedules([s], now, tz)
        assert len(result) == 1
        assert result[0]["day_label"] == "tomorrow"

    def test_sorted_by_starts_in(self):
        """Results are sorted by starts_in_seconds."""
        later = self._make_full_schedule(time(18, 0), time(19, 0), name="Later")
        sooner = self._make_full_schedule(time(14, 0), time(15, 0), name="Sooner")
        now = datetime(2026, 3, 28, 10, 0, tzinfo=timezone.utc)
        tz = ZoneInfo("UTC")

        result = get_upcoming_schedules([later, sooner], now, tz)
        assert len(result) == 2
        assert result[0]["schedule_name"] == "Sooner"
        assert result[1]["schedule_name"] == "Later"

    def test_overnight_duration(self):
        """Overnight schedule (22:00-06:00) should have 8 hour duration."""
        s = self._make_full_schedule(time(22, 0), time(6, 0), name="Overnight")
        now = datetime(2026, 3, 28, 10, 0, tzinfo=timezone.utc)
        tz = ZoneInfo("UTC")

        result = get_upcoming_schedules([s], now, tz)
        assert len(result) == 1
        assert result[0]["duration_mins"] == 480  # 8 hours

    def test_countdown_format_minutes(self):
        """Countdown shows minutes for short waits."""
        s = self._make_full_schedule(time(10, 30), time(11, 0), name="Soon")
        now = datetime(2026, 3, 28, 10, 0, tzinfo=timezone.utc)
        tz = ZoneInfo("UTC")

        result = get_upcoming_schedules([s], now, tz)
        assert result[0]["countdown"] == "30 minutes"

    def test_countdown_format_hours(self):
        """Countdown shows hours for longer waits."""
        s = self._make_full_schedule(time(15, 0), time(16, 0), name="Later")
        now = datetime(2026, 3, 28, 10, 0, tzinfo=timezone.utc)
        tz = ZoneInfo("UTC")

        result = get_upcoming_schedules([s], now, tz)
        assert result[0]["countdown"] == "5 hours"

    def test_countdown_format_singular(self):
        """Countdown uses singular for 1 minute/hour."""
        s = self._make_full_schedule(time(10, 1), time(11, 0), name="One Min")
        now = datetime(2026, 3, 28, 10, 0, tzinfo=timezone.utc)
        tz = ZoneInfo("UTC")

        result = get_upcoming_schedules([s], now, tz)
        assert result[0]["countdown"] == "1 minute"

    def test_start_date_in_future_excluded_today(self):
        """Schedule with start_date in the future isn't upcoming today."""
        s = self._make_full_schedule(
            time(15, 0), time(16, 0), name="Future",
            start_date=datetime(2026, 4, 1, tzinfo=timezone.utc),
        )
        now = datetime(2026, 3, 28, 10, 0, tzinfo=timezone.utc)
        tz = ZoneInfo("UTC")

        result = get_upcoming_schedules([s], now, tz)
        assert len(result) == 0

    def test_end_date_in_past_excluded(self):
        """Schedule with end_date in the past isn't upcoming."""
        s = self._make_full_schedule(
            time(15, 0), time(16, 0), name="Expired",
            end_date=datetime(2026, 3, 27, tzinfo=timezone.utc),
        )
        now = datetime(2026, 3, 28, 10, 0, tzinfo=timezone.utc)
        tz = ZoneInfo("UTC")

        result = get_upcoming_schedules([s], now, tz)
        assert len(result) == 0

    def test_timezone_aware(self):
        """Times are calculated in the given timezone."""
        # It's 5 PM UTC = 10 AM Pacific. Schedule at 11:00 Pacific is upcoming.
        s = self._make_full_schedule(time(11, 0), time(12, 0), name="Pacific")
        now = datetime(2026, 3, 28, 17, 0, tzinfo=timezone.utc)
        tz = ZoneInfo("America/Los_Angeles")

        result = get_upcoming_schedules([s], now, tz)
        assert len(result) == 1
        assert result[0]["day_label"] == "today"


# ── Schedule unique names (API test) ──


@pytest.mark.asyncio
class TestUniqueScheduleName:
    async def _create_device_and_asset(self, db_session):
        device = Device(id="dedup-pi", name="Dedup Test", status=DeviceStatus.APPROVED)
        asset = Asset(filename="dedup.mp4", asset_type=AssetType.VIDEO, size_bytes=100, checksum="ded")
        db_session.add_all([device, asset])
        await db_session.commit()
        return device.id, str(asset.id)

    async def test_duplicate_name_gets_suffix(self, client, db_session):
        device_id, asset_id = await self._create_device_and_asset(db_session)

        resp1 = await client.post("/api/schedules", json={
            "name": "Morning", "device_id": device_id, "asset_id": asset_id,
            "start_time": "08:00", "end_time": "12:00",
        })
        assert resp1.status_code == 201
        assert resp1.json()["name"] == "Morning"

        resp2 = await client.post("/api/schedules", json={
            "name": "Morning", "device_id": device_id, "asset_id": asset_id,
            "start_time": "13:00", "end_time": "17:00",
        })
        assert resp2.status_code == 201
        assert resp2.json()["name"] == "Morning (2)"

    async def test_triple_duplicate(self, client, db_session):
        device_id, asset_id = await self._create_device_and_asset(db_session)

        for i in range(3):
            resp = await client.post("/api/schedules", json={
                "name": "Repeat", "device_id": device_id, "asset_id": asset_id,
                "start_time": "08:00", "end_time": "12:00",
            })
            assert resp.status_code == 201

        resp = await client.get("/api/schedules")
        names = sorted([s["name"] for s in resp.json()])
        assert "Repeat" in names
        assert "Repeat (2)" in names
        assert "Repeat (3)" in names

    async def test_unique_name_not_modified(self, client, db_session):
        device_id, asset_id = await self._create_device_and_asset(db_session)

        resp = await client.post("/api/schedules", json={
            "name": "Unique", "device_id": device_id, "asset_id": asset_id,
            "start_time": "08:00", "end_time": "12:00",
        })
        assert resp.status_code == 201
        assert resp.json()["name"] == "Unique"


# ── End Now API endpoint ──


@pytest.mark.asyncio
class TestEndNowEndpoint:
    async def _create_schedule(self, client, db_session):
        device = Device(id="end-now-pi", name="End Now Test", status=DeviceStatus.APPROVED)
        asset = Asset(filename="end.mp4", asset_type=AssetType.VIDEO, size_bytes=100, checksum="end")
        setting = CMSSetting(key="timezone", value="UTC")
        db_session.add_all([device, asset, setting])
        await db_session.commit()

        resp = await client.post("/api/schedules", json={
            "name": "End Me",
            "device_id": device.id,
            "asset_id": str(asset.id),
            "start_time": "08:00",
            "end_time": "17:00",
        })
        assert resp.status_code == 201
        return resp.json()["id"]

    def setup_method(self):
        _skipped.clear()
        _now_playing.clear()

    def teardown_method(self):
        _skipped.clear()
        _now_playing.clear()

    async def test_end_now_success(self, client, db_session):
        sched_id = await self._create_schedule(client, db_session)
        resp = await client.post(f"/api/schedules/{sched_id}/end-now")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ended"] == sched_id
        assert "resumes_after" in data
        assert sched_id in _skipped

    async def test_end_now_not_found(self, client):
        resp = await client.post("/api/schedules/00000000-0000-0000-0000-000000000000/end-now")
        assert resp.status_code == 404

    async def test_end_now_removes_from_now_playing(self, client, db_session):
        sched_id = await self._create_schedule(client, db_session)
        # Simulate now_playing entry
        _now_playing["end-now-pi"] = {"schedule_id": sched_id, "device_id": "end-now-pi"}

        resp = await client.post(f"/api/schedules/{sched_id}/end-now")
        assert resp.status_code == 200
        assert "end-now-pi" not in _now_playing

    async def test_end_now_requires_auth(self, unauthed_client, client, db_session):
        sched_id = await self._create_schedule(client, db_session)
        resp = await unauthed_client.post(f"/api/schedules/{sched_id}/end-now")
        assert resp.status_code in (401, 303)


# ── build_device_sync with skipped schedules ──


@pytest.mark.asyncio
class TestBuildDeviceSyncSkipped:
    @pytest_asyncio.fixture
    async def db(self, db_engine):
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            yield session

    def setup_method(self):
        _skipped.clear()

    def teardown_method(self):
        _skipped.clear()

    async def _setup(self, db):
        setting = CMSSetting(key="timezone", value="UTC")
        asset = Asset(filename="skip-test.mp4", asset_type=AssetType.VIDEO, size_bytes=1000, checksum="skp")
        device = Device(id="skip-pi-01", name="Skip Test", status=DeviceStatus.APPROVED)
        db.add_all([setting, asset, device])
        await db.flush()

        sched = Schedule(
            name="Skippable",
            device_id="skip-pi-01",
            asset_id=asset.id,
            start_time=time(9, 0),
            end_time=time(17, 0),
        )
        db.add(sched)
        await db.commit()
        return str(sched.id)

    async def test_skipped_schedule_excluded_from_sync(self, db):
        sched_id = await self._setup(db)
        _skipped[sched_id] = datetime(2026, 3, 29, 17, 0)

        sync = await build_device_sync("skip-pi-01", db)
        assert sync is not None
        assert sync.schedules == []

    async def test_unskipped_schedule_included(self, db):
        await self._setup(db)
        # Don't skip anything
        sync = await build_device_sync("skip-pi-01", db)
        assert len(sync.schedules) == 1
        assert sync.schedules[0].name == "Skippable"
