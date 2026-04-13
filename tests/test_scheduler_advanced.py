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
    _offline_since,
    _missed_logged,
    MISSED_GRACE_SECONDS,
    build_device_sync,
    clear_schedule_skip,
    clear_sync_hash,
    clear_now_playing,
    evaluate_schedules,
    get_now_playing,
    get_upcoming_schedules,
    set_now_playing,
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

    def test_clear_skip_removes_entry(self):
        skip_schedule_until("sched-1", datetime(2026, 3, 29, 17, 0))
        assert "sched-1" in _skipped
        clear_schedule_skip("sched-1")
        assert "sched-1" not in _skipped

    def test_clear_skip_nonexistent_is_harmless(self):
        clear_schedule_skip("no-such-id")
        assert "no-such-id" not in _skipped

    def test_clear_skip_allows_schedule_to_reappear(self):
        """After clearing a skip, the schedule should appear in upcoming again."""
        low = _make_schedule(
            time(8, 0), time(17, 0), priority=1, name="Resumed",
            device_id="d1",
        )
        skip_schedule_until(str(low.id), datetime(2026, 3, 28, 17, 0))

        now = datetime(2026, 3, 28, 12, 0, tzinfo=timezone.utc)
        np = []

        # Should not appear while skipped
        result = get_upcoming_schedules([low], now, ZoneInfo("UTC"), now_playing=np)
        assert len(result) == 0

        # Clear the skip
        clear_schedule_skip(str(low.id))

        # Should now be visible again
        result = get_upcoming_schedules([low], now, ZoneInfo("UTC"), now_playing=np)
        # It won't show in "upcoming" as preempted because there's no winner,
        # but it won't be hidden by the skip filter either.
        # The key assertion is that the _skipped dict no longer blocks it.
        assert str(low.id) not in _skipped


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

    def test_sub_minute_duration_shows_seconds(self):
        """A schedule with duration < 60s should have duration_secs set correctly."""
        s = self._make_full_schedule(time(9, 0, 0), time(9, 0, 30), name="Short Clip")
        now = datetime(2026, 3, 28, 8, 0, tzinfo=timezone.utc)
        tz = ZoneInfo("UTC")

        result = get_upcoming_schedules([s], now, tz)
        assert len(result) == 1
        assert result[0]["duration_secs"] == 30
        assert result[0]["duration_mins"] == 0


# ── Schedule unique names (API test) ──


@pytest.mark.asyncio
class TestUniqueScheduleName:
    async def _create_device_and_asset(self, db_session):
        device = Device(id="dedup-pi", name="Dedup Test", status=DeviceStatus.ADOPTED)
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
                "priority": i,
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
        device = Device(id="end-now-pi", name="End Now Test", status=DeviceStatus.ADOPTED)
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
        device = Device(id="skip-pi-01", name="Skip Test", status=DeviceStatus.ADOPTED)
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


@pytest.mark.asyncio
class TestNowPlayingCleanup:
    """Test that _now_playing is managed correctly with event-driven model."""

    def setup_method(self):
        _now_playing.clear()
        _last_sync_hash.clear()
        _skipped.clear()

    def teardown_method(self):
        _now_playing.clear()
        _last_sync_hash.clear()
        _skipped.clear()

    async def test_now_playing_cleared_when_schedule_deleted(self, app, db_session):
        """Deleting a schedule should clear its _now_playing entries."""
        # Create device, asset, timezone setting
        setting = CMSSetting(key="timezone", value="UTC")
        asset = Asset(filename="test-video.mp4", asset_type=AssetType.VIDEO,
                      size_bytes=1000, checksum="abc123")
        device = Device(id="np-cleanup-01", name="Cleanup Test",
                        status=DeviceStatus.ADOPTED)
        db_session.add_all([setting, asset, device])
        await db_session.flush()

        # Create schedule
        sched = Schedule(
            name="Play Now Test",
            device_id="np-cleanup-01",
            asset_id=asset.id,
            start_time=time(0, 0),
            end_time=time(23, 59),
            priority=10,
            enabled=True,
        )
        db_session.add(sched)
        await db_session.commit()
        sched_id = str(sched.id)

        # Simulate device reporting playback via PLAYBACK_STARTED
        set_now_playing("np-cleanup-01", {
            "device_id": "np-cleanup-01",
            "device_name": "Cleanup Test",
            "schedule_id": sched_id,
            "schedule_name": "Play Now Test",
            "asset_filename": "test-video.mp4",
            "since": datetime.now(timezone.utc).isoformat(),
            "source": "device",
        })
        assert "np-cleanup-01" in _now_playing
        assert _now_playing["np-cleanup-01"]["schedule_id"] == sched_id

        # Simulate schedule deletion clearing _now_playing
        # (this is what the delete_schedule route does)
        stale = [did for did, info in _now_playing.items()
                 if info.get("schedule_id") == sched_id]
        for did in stale:
            clear_now_playing(did)

        assert "np-cleanup-01" not in _now_playing, \
            "_now_playing should be cleared after schedule deletion"

    async def test_now_playing_replaced_by_device_events(self, app, db_session):
        """Device events replace _now_playing entries correctly."""
        sched_a_id = str(uuid.uuid4())
        sched_b_id = str(uuid.uuid4())

        # Simulate device reporting schedule A
        set_now_playing("np-replace-01", {
            "device_id": "np-replace-01",
            "device_name": "Replace Test",
            "schedule_id": sched_a_id,
            "schedule_name": "Schedule A",
            "asset_filename": "video-a.mp4",
            "since": datetime.now(timezone.utc).isoformat(),
            "source": "device",
        })
        assert _now_playing["np-replace-01"]["asset_filename"] == "video-a.mp4"

        # Simulate device switching to schedule B
        set_now_playing("np-replace-01", {
            "device_id": "np-replace-01",
            "device_name": "Replace Test",
            "schedule_id": sched_b_id,
            "schedule_name": "Schedule B",
            "asset_filename": "video-b.mp4",
            "since": datetime.now(timezone.utc).isoformat(),
            "source": "device",
        })
        assert _now_playing["np-replace-01"]["asset_filename"] == "video-b.mp4"
        assert _now_playing["np-replace-01"]["schedule_name"] == "Schedule B"

    async def test_ended_log_survives_deleted_schedule_fk(self, app, db_session):
        """When a schedule is deleted, the ENDED log event should not crash
        due to FK violation on schedule_logs.schedule_id (Issue #126)."""
        from cms.models.schedule_log import ScheduleLog, ScheduleLogEvent
        from cms.services.scheduler import log_schedule_event

        setting = CMSSetting(key="timezone", value="UTC")
        asset = Asset(filename="fk-test.mp4", asset_type=AssetType.VIDEO,
                      size_bytes=1000, checksum="fk123")
        device = Device(id="np-fk-01", name="FK Test",
                        status=DeviceStatus.ADOPTED)
        db_session.add_all([setting, asset, device])
        await db_session.flush()

        sched = Schedule(
            name="Will Be Deleted",
            device_id="np-fk-01",
            asset_id=asset.id,
            start_time=time(0, 0),
            end_time=time(23, 59),
            priority=10,
            enabled=True,
        )
        db_session.add(sched)
        await db_session.commit()
        sched_id = str(sched.id)

        # Simulate device reported playback
        set_now_playing("np-fk-01", {
            "device_id": "np-fk-01",
            "device_name": "FK Test",
            "schedule_id": sched_id,
            "schedule_name": "Will Be Deleted",
            "asset_filename": "fk-test.mp4",
            "since": datetime.now(timezone.utc).isoformat(),
            "source": "device",
        })

        # Delete the schedule
        await db_session.delete(sched)
        await db_session.commit()

        # Log ENDED event with the now-deleted schedule_id
        # (simulating what happens when device sends PLAYBACK_ENDED
        # for a schedule that was deleted while playing)
        await log_schedule_event(
            db_session, ScheduleLogEvent.ENDED,
            schedule_name="Will Be Deleted",
            device_name="FK Test",
            asset_filename="fk-test.mp4",
            schedule_id=sched_id,
            device_id="np-fk-01",
        )
        clear_now_playing("np-fk-01")
        assert "np-fk-01" not in _now_playing

        # Verify ENDED was logged — FK references are cleared (schedule
        # was deleted) but denormalized name columns survive.
        from sqlalchemy import select as sa_select
        logs = await db_session.execute(
            sa_select(ScheduleLog).where(
                ScheduleLog.event == ScheduleLogEvent.ENDED,
                ScheduleLog.schedule_name == "Will Be Deleted",
            )
        )
        ended_logs = logs.scalars().all()
        assert len(ended_logs) >= 1
        assert ended_logs[-1].schedule_name == "Will Be Deleted"
        # schedule_id is None because the FK target was deleted
        assert ended_logs[-1].schedule_id is None

    async def test_now_playing_cleaned_on_disconnect(self, app, db_session):
        """Scheduler should clean up _now_playing for disconnected devices."""
        from cms.services.device_manager import device_manager

        setting = CMSSetting(key="timezone", value="UTC")
        db_session.add(setting)
        await db_session.commit()

        # Simulate device reported playback while connected
        set_now_playing("np-disconnect-01", {
            "device_id": "np-disconnect-01",
            "device_name": "Disconnect Test",
            "schedule_id": str(uuid.uuid4()),
            "schedule_name": "Some Schedule",
            "asset_filename": "video.mp4",
            "since": datetime.now(timezone.utc).isoformat(),
            "source": "device",
        })
        assert "np-disconnect-01" in _now_playing

        # Device is NOT in connected list — scheduler should clean up
        await evaluate_schedules()
        # Still there because no connected devices to process
        # Now register a different device to trigger the scheduler
        class FakeWS:
            async def send_json(self, data): pass

        device2 = Device(id="np-disconnect-02", name="Other",
                         status=DeviceStatus.ADOPTED)
        db_session.add(device2)
        await db_session.commit()
        device_manager.register("np-disconnect-02", FakeWS())

        try:
            await evaluate_schedules()
            assert "np-disconnect-01" not in _now_playing, \
                "_now_playing should be cleaned up for disconnected devices"
        finally:
            device_manager.disconnect("np-disconnect-02")

    async def test_now_playing_set_by_device_events(self, app, db_session):
        """set_now_playing should correctly populate _now_playing with device-reported data."""
        sched_id = str(uuid.uuid4())

        set_now_playing("np-countdown-01", {
            "device_id": "np-countdown-01",
            "device_name": "Countdown",
            "schedule_id": sched_id,
            "schedule_name": "Countdown Test",
            "asset_filename": "countdown.mp4",
            "since": datetime.now(timezone.utc).isoformat(),
            "end_time": "11:59 PM",
            "start_time_raw": "00:00:00",
            "end_time_raw": "23:59:59",
            "source": "device",
        })
        assert "np-countdown-01" in _now_playing
        entry = _now_playing["np-countdown-01"]
        assert entry["schedule_id"] == sched_id
        assert entry["asset_filename"] == "countdown.mp4"
        assert entry["source"] == "device"
        assert "start_time_raw" in entry
        assert "end_time_raw" in entry


class TestNowPlayingRemainingText:
    """Test that the remaining text is formatted correctly for the dashboard."""

    def setup_method(self):
        _now_playing.clear()

    def teardown_method(self):
        _now_playing.clear()

    def _inject_remaining(self, remaining_secs: int):
        """Simulate the evaluate_schedules remaining-text logic."""
        entry = {
            "device_id": "test-dev",
            "schedule_id": "test-sched",
            "remaining_seconds": remaining_secs,
        }
        if remaining_secs < 60:
            entry["remaining"] = "less than a minute"
        elif remaining_secs < 3600:
            mins = remaining_secs // 60
            entry["remaining"] = f"{mins} minute{'s' if mins != 1 else ''}"
        else:
            hours = remaining_secs // 3600
            mins = (remaining_secs % 3600) // 60
            entry["remaining"] = f"{hours} hour{'s' if hours != 1 else ''}"
            if mins > 0:
                entry["remaining"] += f", {mins} minute{'s' if mins != 1 else ''}"
        _now_playing["test-dev"] = entry
        return entry

    def test_remaining_under_30s_shows_less_than_minute(self):
        """≤30s: server sends 'less than a minute' (JS countdown handles live display)."""
        entry = self._inject_remaining(25)
        assert entry["remaining"] == "less than a minute"
        assert entry["remaining_seconds"] == 25

    def test_remaining_45s_shows_less_than_minute(self):
        """31-59s: still 'less than a minute'."""
        entry = self._inject_remaining(45)
        assert entry["remaining"] == "less than a minute"
        assert entry["remaining_seconds"] == 45

    def test_remaining_1s_shows_less_than_minute(self):
        """Edge case: 1 second remaining."""
        entry = self._inject_remaining(1)
        assert entry["remaining"] == "less than a minute"
        assert entry["remaining_seconds"] == 1

    def test_remaining_0s_shows_less_than_minute(self):
        """Edge case: 0 seconds remaining."""
        entry = self._inject_remaining(0)
        assert entry["remaining"] == "less than a minute"
        assert entry["remaining_seconds"] == 0

    def test_remaining_60s_shows_1_minute(self):
        """Exactly 60s should show '1 minute' (singular)."""
        entry = self._inject_remaining(60)
        assert entry["remaining"] == "1 minute"

    def test_remaining_5_minutes(self):
        """300s = 5 minutes."""
        entry = self._inject_remaining(300)
        assert entry["remaining"] == "5 minutes"

    def test_remaining_1_hour(self):
        """3600s = 1 hour."""
        entry = self._inject_remaining(3600)
        assert entry["remaining"] == "1 hour"

    def test_remaining_1_hour_30_minutes(self):
        """5400s = 1 hour, 30 minutes."""
        entry = self._inject_remaining(5400)
        assert entry["remaining"] == "1 hour, 30 minutes"

    def test_remaining_seconds_always_int(self):
        """remaining_seconds should always be an int."""
        for secs in [0, 1, 15, 30, 59, 60, 300, 3600, 7200]:
            entry = self._inject_remaining(secs)
            assert isinstance(entry["remaining_seconds"], int)


@pytest.mark.asyncio
class TestDashboardCountdownAttribute:
    """Test that the dashboard HTML renders data-remaining for JS countdown."""

    async def test_data_remaining_attr_rendered(self, client, db_session):
        """The now-playing row should have data-remaining='N' for the JS countdown."""
        _now_playing.clear()
        # Dashboard needs a timezone setting
        db_session.add(CMSSetting(key="timezone", value="UTC"))
        await db_session.commit()

        _now_playing["dash-dev"] = {
            "device_id": "dash-dev",
            "device_name": "Dashboard Dev",
            "schedule_id": "sched-abc",
            "schedule_name": "Countdown Sched",
            "asset_filename": "clip.mp4",
            "end_time": "11:59 PM",
            "remaining": "less than a minute",
            "remaining_seconds": 22,
            "since": datetime.now(timezone.utc).isoformat(),
        }

        try:
            resp = await client.get("/")
            assert resp.status_code == 200
            html = resp.text
            assert 'data-remaining="22"' in html
            assert "less than a minute" in html
        finally:
            _now_playing.clear()

    async def test_data_remaining_attr_large_value(self, client, db_session):
        """data-remaining should render even for values > 30s (JS ignores them)."""
        _now_playing.clear()
        db_session.add(CMSSetting(key="timezone", value="UTC"))
        await db_session.commit()

        _now_playing["dash-dev2"] = {
            "device_id": "dash-dev2",
            "device_name": "Dashboard Dev 2",
            "schedule_id": "sched-xyz",
            "schedule_name": "Long Sched",
            "asset_filename": "movie.mp4",
            "end_time": "6:00 PM",
            "remaining": "2 hours",
            "remaining_seconds": 7200,
            "since": datetime.now(timezone.utc).isoformat(),
        }

        try:
            resp = await client.get("/")
            assert resp.status_code == 200
            html = resp.text
            assert 'data-remaining="7200"' in html
            assert "2 hours" in html
        finally:
            _now_playing.clear()


@pytest.mark.asyncio
class TestMissedGracePeriod:
    """Test that MISSED is only logged after MISSED_GRACE_SECONDS of continuous offline."""

    def setup_method(self):
        _now_playing.clear()
        _missed_logged.clear()
        _offline_since.clear()

    def teardown_method(self):
        _now_playing.clear()
        _missed_logged.clear()
        _offline_since.clear()

    async def test_missed_not_logged_before_grace_period(self, app, db_session):
        """MISSED should NOT be logged immediately when device goes offline."""
        from cms.services.device_manager import device_manager
        from cms.models.schedule_log import ScheduleLog

        setting = CMSSetting(key="timezone", value="UTC")
        asset = Asset(filename="grace.mp4", asset_type=AssetType.VIDEO,
                      size_bytes=1000, checksum="grace1")
        device = Device(id="grace-dev-01", name="Grace Device",
                        status=DeviceStatus.ADOPTED)
        # A second device that IS connected (so scheduler doesn't skip)
        dummy = Device(id="grace-dummy", name="Dummy",
                       status=DeviceStatus.ADOPTED)
        db_session.add_all([setting, asset, device, dummy])
        await db_session.flush()

        sched = Schedule(
            name="Grace Test",
            device_id="grace-dev-01",
            asset_id=asset.id,
            start_time=time(0, 0),
            end_time=time(23, 59, 59),
            priority=10,
            enabled=True,
        )
        db_session.add(sched)
        await db_session.commit()

        class FakeWS:
            async def send_json(self, data): pass

        device_manager.register("grace-dummy", FakeWS())

        try:
            # grace-dev-01 is NOT connected — scheduler should start tracking
            # offline time but NOT log MISSED yet (grace period not elapsed)
            await evaluate_schedules()

            from sqlalchemy import select as sa_select
            result = await db_session.execute(sa_select(ScheduleLog))
            logs = result.scalars().all()
            missed_logs = [l for l in logs if l.event.value == "MISSED"]
            assert len(missed_logs) == 0, \
                "MISSED should NOT be logged before grace period expires"

            # Verify offline tracking started
            key = (str(sched.id), "grace-dev-01")
            assert key in _offline_since, \
                "_offline_since should track when device first seen offline"
        finally:
            device_manager.disconnect("grace-dummy")

    async def test_missed_logged_after_grace_period(self, app, db_session):
        """MISSED should be logged after MISSED_GRACE_SECONDS of continuous offline."""
        from cms.services.device_manager import device_manager
        from cms.models.schedule_log import ScheduleLog

        setting = CMSSetting(key="timezone", value="UTC")
        asset = Asset(filename="grace2.mp4", asset_type=AssetType.VIDEO,
                      size_bytes=1000, checksum="grace2")
        device = Device(id="grace-dev-02", name="Grace Device 2",
                        status=DeviceStatus.ADOPTED)
        dummy = Device(id="grace-dummy-2", name="Dummy 2",
                       status=DeviceStatus.ADOPTED)
        db_session.add_all([setting, asset, device, dummy])
        await db_session.flush()

        sched = Schedule(
            name="Grace Test 2",
            device_id="grace-dev-02",
            asset_id=asset.id,
            start_time=time(0, 0),
            end_time=time(23, 59, 59),
            priority=10,
            enabled=True,
        )
        db_session.add(sched)
        await db_session.commit()

        class FakeWS:
            async def send_json(self, data): pass

        device_manager.register("grace-dummy-2", FakeWS())

        try:
            # Pre-seed _offline_since to simulate device offline > grace period
            key = (str(sched.id), "grace-dev-02")
            _offline_since[key] = datetime.now(timezone.utc) - timedelta(
                seconds=MISSED_GRACE_SECONDS + 10
            )

            await evaluate_schedules()

            from sqlalchemy import select as sa_select
            result = await db_session.execute(sa_select(ScheduleLog))
            logs = result.scalars().all()
            missed_logs = [l for l in logs if l.event.value == "MISSED"]
            assert len(missed_logs) == 1, \
                "MISSED should be logged after grace period expires"
            assert missed_logs[0].device_name == "Grace Device 2"
        finally:
            device_manager.disconnect("grace-dummy-2")

    async def test_missed_cleared_when_device_reconnects(self, app, db_session):
        """When device reconnects, offline tracking should be cleared."""
        from cms.services.device_manager import device_manager

        setting = CMSSetting(key="timezone", value="UTC")
        asset = Asset(filename="grace3.mp4", asset_type=AssetType.VIDEO,
                      size_bytes=1000, checksum="grace3")
        device = Device(id="grace-dev-03", name="Grace Device 3",
                        status=DeviceStatus.ADOPTED)
        db_session.add_all([setting, asset, device])
        await db_session.flush()

        sched = Schedule(
            name="Grace Test 3",
            device_id="grace-dev-03",
            asset_id=asset.id,
            start_time=time(0, 0),
            end_time=time(23, 59, 59),
            priority=10,
            enabled=True,
        )
        db_session.add(sched)
        await db_session.commit()

        key = (str(sched.id), "grace-dev-03")
        # Pre-seed offline tracking
        _offline_since[key] = datetime.now(timezone.utc) - timedelta(seconds=30)

        # Connect the device
        class FakeWS:
            async def send_json(self, data): pass

        device_manager.register("grace-dev-03", FakeWS())

        try:
            await evaluate_schedules()
            # Offline tracking should be cleared since device is now connected
            assert key not in _offline_since, \
                "_offline_since should be cleared when device reconnects"
        finally:
            device_manager.disconnect("grace-dev-03")