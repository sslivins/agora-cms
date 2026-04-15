"""Tests for schedule history logging (ScheduleLog model and event logging)."""

from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from cms.models.asset import Asset, AssetType
from cms.models.device import Device, DeviceGroup, DeviceStatus
from cms.models.schedule import Schedule
from cms.models.schedule_log import ScheduleLog, ScheduleLogEvent


@pytest.mark.asyncio
class TestScheduleLogModel:
    """Test ScheduleLog ORM model CRUD."""

    @pytest_asyncio.fixture
    async def db(self, db_engine):
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            yield session

    async def test_create_log_entry(self, db):
        entry = ScheduleLog(
            schedule_name="Morning Loop",
            device_name="Lobby TV",
            asset_filename="promo.mp4",
            event=ScheduleLogEvent.STARTED,
        )
        db.add(entry)
        await db.commit()

        result = await db.execute(select(ScheduleLog))
        rows = result.scalars().all()
        assert len(rows) == 1
        assert rows[0].schedule_name == "Morning Loop"
        assert rows[0].event == ScheduleLogEvent.STARTED
        assert rows[0].timestamp is not None

    async def test_all_event_types(self, db):
        for event_type in ScheduleLogEvent:
            db.add(ScheduleLog(
                schedule_name=f"Test {event_type.value}",
                device_name="Dev",
                asset_filename="v.mp4",
                event=event_type,
            ))
        await db.commit()

        result = await db.execute(select(ScheduleLog))
        rows = result.scalars().all()
        assert len(rows) == 4
        events = {r.event for r in rows}
        assert events == {
            ScheduleLogEvent.STARTED,
            ScheduleLogEvent.ENDED,
            ScheduleLogEvent.SKIPPED,
            ScheduleLogEvent.MISSED,
        }

    async def test_optional_foreign_keys(self, db):
        """schedule_id and device_id can be None (denormalized for survivability)."""
        entry = ScheduleLog(
            schedule_id=None,
            device_id=None,
            schedule_name="Deleted Schedule",
            device_name="Removed Device",
            asset_filename="old.mp4",
            event=ScheduleLogEvent.ENDED,
            details="Original records were deleted",
        )
        db.add(entry)
        await db.commit()

        result = await db.execute(select(ScheduleLog))
        row = result.scalar_one()
        assert row.schedule_id is None
        assert row.device_id is None
        assert row.details == "Original records were deleted"

    async def test_with_foreign_keys(self, db):
        """schedule_id and device_id link to real records when available."""
        group = DeviceGroup(name="Log Test Group")
        device = Device(id="log-pi", name="Log Test", status=DeviceStatus.ADOPTED)
        asset = Asset(filename="clip.mp4", asset_type=AssetType.VIDEO, size_bytes=100, checksum="abc")
        db.add_all([group, device, asset])
        await db.flush()
        device.group_id = group.id

        schedule = Schedule(
            name="Linked Schedule",
            group_id=group.id,
            asset_id=asset.id,
            start_time=datetime.strptime("08:00", "%H:%M").time(),
            end_time=datetime.strptime("12:00", "%H:%M").time(),
        )
        db.add(schedule)
        await db.flush()

        entry = ScheduleLog(
            schedule_id=schedule.id,
            schedule_name=schedule.name,
            device_id=device.id,
            device_name=device.name,
            asset_filename=asset.filename,
            event=ScheduleLogEvent.STARTED,
        )
        db.add(entry)
        await db.commit()

        result = await db.execute(select(ScheduleLog))
        row = result.scalar_one()
        assert row.schedule_id == schedule.id
        assert row.device_id == device.id


@pytest.mark.asyncio
class TestLogEventHelper:
    """Test the _log_event helper in scheduler.py."""

    @pytest_asyncio.fixture
    async def db(self, db_engine):
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            yield session

    async def test_log_event_writes_entry(self, db):
        from cms.services.scheduler import _log_event

        await _log_event(
            db,
            ScheduleLogEvent.MISSED,
            schedule_name="Evening Show",
            device_name="Lobby TV",
            asset_filename="show.mp4",
            details="Device offline",
        )
        await db.commit()

        result = await db.execute(select(ScheduleLog))
        row = result.scalar_one()
        assert row.event == ScheduleLogEvent.MISSED
        assert row.schedule_name == "Evening Show"
        assert row.device_name == "Lobby TV"
        assert row.asset_filename == "show.mp4"
        assert row.details == "Device offline"

    async def test_log_event_with_ids(self, db):
        from cms.services.scheduler import _log_event

        group = DeviceGroup(name="Log Event Group")
        device = Device(id="log-dev-1", name="Log Dev", status=DeviceStatus.ADOPTED)
        asset = Asset(filename="log.mp4", asset_type=AssetType.VIDEO, size_bytes=100, checksum="log1")
        db.add_all([group, device, asset])
        await db.flush()
        device.group_id = group.id

        schedule = Schedule(
            name="Test",
            group_id=group.id,
            asset_id=asset.id,
            start_time=datetime.strptime("08:00", "%H:%M").time(),
            end_time=datetime.strptime("12:00", "%H:%M").time(),
        )
        db.add(schedule)
        await db.flush()

        await _log_event(
            db,
            ScheduleLogEvent.STARTED,
            schedule_name="Test",
            device_name="Dev",
            asset_filename="v.mp4",
            schedule_id=schedule.id,
            device_id=device.id,
        )
        await db.commit()

        result = await db.execute(select(ScheduleLog))
        row = result.scalar_one()
        assert row.schedule_id == schedule.id
        assert row.device_id == device.id


@pytest.mark.asyncio
class TestEndNowLogsSkipped:
    """Test that the end-now API endpoint logs SKIPPED events."""

    async def _seed(self, db_session):
        group = DeviceGroup(name="Skip Group")
        device = Device(id="skip-pi", name="Skip Test", status=DeviceStatus.ADOPTED)
        asset = Asset(filename="skip-video.mp4", asset_type=AssetType.VIDEO, size_bytes=100, checksum="ccc")
        db_session.add_all([group, device, asset])
        await db_session.flush()
        device.group_id = group.id

        schedule = Schedule(
            name="Skippable Schedule",
            group_id=group.id,
            asset_id=asset.id,
            start_time=datetime.strptime("00:00", "%H:%M").time(),
            end_time=datetime.strptime("23:59", "%H:%M").time(),
        )
        db_session.add(schedule)
        await db_session.commit()
        return str(schedule.id)

    async def test_end_now_creates_skipped_log(self, client, db_session):
        schedule_id = await self._seed(db_session)
        resp = await client.post(f"/api/schedules/{schedule_id}/end-now")
        assert resp.status_code == 200

        result = await db_session.execute(select(ScheduleLog))
        logs = result.scalars().all()
        assert len(logs) == 1
        assert logs[0].event == ScheduleLogEvent.SKIPPED
        assert logs[0].schedule_name == "Skippable Schedule"
        assert logs[0].device_name == "Skip Test"
        assert logs[0].asset_filename == "skip-video.mp4"
        assert logs[0].details == "Ended early by admin"


@pytest.mark.asyncio
class TestHistoryUI:
    """Test the /history page and dashboard recent activity."""

    async def test_history_page_loads(self, client):
        resp = await client.get("/history")
        assert resp.status_code == 200
        assert "history" in resp.text.lower()

    async def test_history_page_shows_logs(self, client, db_session):
        db_session.add(ScheduleLog(
            schedule_name="Visible Schedule",
            device_name="Visible Device",
            asset_filename="visible.mp4",
            event=ScheduleLogEvent.STARTED,
        ))
        await db_session.commit()

        resp = await client.get("/history")
        assert resp.status_code == 200
        assert "Visible Schedule" in resp.text
        assert "Visible Device" in resp.text

    async def test_dashboard_recent_activity(self, client, db_session):
        db_session.add(ScheduleLog(
            schedule_name="Dashboard Schedule",
            device_name="Dashboard Device",
            asset_filename="dash.mp4",
            event=ScheduleLogEvent.ENDED,
        ))
        await db_session.commit()

        resp = await client.get("/")
        assert resp.status_code == 200
        assert "Dashboard Schedule" in resp.text

    async def test_dashboard_json_activity_count(self, client, db_session):
        db_session.add(ScheduleLog(
            schedule_name="Count Test",
            device_name="Dev",
            asset_filename="c.mp4",
            event=ScheduleLogEvent.STARTED,
        ))
        await db_session.commit()

        resp = await client.get("/api/dashboard")
        assert resp.status_code == 200
        data = resp.json()
        assert "activity_count" in data
        assert data["activity_count"] >= 1

    async def test_history_requires_auth(self, unauthed_client):
        resp = await unauthed_client.get("/history", follow_redirects=False)
        assert resp.status_code in (401, 303)

    async def test_history_pagination(self, client, db_session):
        """History page should paginate at 50 per page."""
        for i in range(55):
            db_session.add(ScheduleLog(
                schedule_name=f"Sched {i}",
                device_name="Dev",
                asset_filename="a.mp4",
                event=ScheduleLogEvent.STARTED,
            ))
        await db_session.commit()

        # Page 1 should show pagination controls
        resp = await client.get("/history")
        assert resp.status_code == 200
        assert "Page 1 of 2" in resp.text
        assert "55 events" in resp.text
        assert "Next" in resp.text

        # Page 2 should have remaining entries and Prev link
        resp2 = await client.get("/history?page=2")
        assert resp2.status_code == 200
        assert "Page 2 of 2" in resp2.text
        assert "Prev" in resp2.text
