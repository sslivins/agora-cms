"""Tests for display_name feature: PATCH endpoint, priority logic, schedule API propagation,
and skipped_until DB persistence across restarts."""

import io
import uuid
from datetime import datetime, time, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select

from cms.models.asset import Asset, AssetType
from cms.models.device import Device, DeviceGroup, DeviceStatus
from cms.models.schedule import Schedule
from cms.services.scheduler import (
    _asset_display_name,
    _skipped,
    _now_playing,
    _ensure_skips_loaded,
)


# ── Helpers ──


def _upload(filename: str, content: bytes = b"fakecontent"):
    return {"file": (filename, io.BytesIO(content), "application/octet-stream")}


async def _create_asset(db_session, filename="test.mp4", display_name=None,
                        original_filename=None) -> Asset:
    asset = Asset(
        filename=filename,
        asset_type=AssetType.VIDEO,
        size_bytes=1000,
        checksum="abc123",
        display_name=display_name,
        original_filename=original_filename,
    )
    db_session.add(asset)
    await db_session.flush()
    return asset


async def _seed_schedule(db_session, asset, group) -> Schedule:
    sched = Schedule(
        name="Test Schedule",
        asset_id=asset.id,
        group_id=group.id,
        start_time=time(0, 0),
        end_time=time(23, 59),
        enabled=True,
        priority=0,
    )
    db_session.add(sched)
    await db_session.flush()
    return sched


async def _seed_group(db_session, name="Test Group") -> DeviceGroup:
    group = DeviceGroup(name=name)
    db_session.add(group)
    await db_session.flush()
    return group


# ── PATCH /api/assets/{id} ──


@pytest.mark.asyncio
class TestPatchAssetDisplayName:
    """Test the PATCH endpoint for setting/clearing display_name."""

    async def test_set_display_name(self, client, db_session):
        asset = await _create_asset(db_session)
        await db_session.commit()

        resp = await client.patch(
            f"/api/assets/{asset.id}",
            json={"display_name": "My Friendly Name"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["display_name"] == "My Friendly Name"

    async def test_clear_display_name(self, client, db_session):
        asset = await _create_asset(db_session, display_name="Old Name")
        await db_session.commit()

        resp = await client.patch(
            f"/api/assets/{asset.id}",
            json={"display_name": ""},
        )
        assert resp.status_code == 200
        assert resp.json()["display_name"] is None

    async def test_display_name_too_long(self, client, db_session):
        asset = await _create_asset(db_session)
        await db_session.commit()

        resp = await client.patch(
            f"/api/assets/{asset.id}",
            json={"display_name": "x" * 256},
        )
        assert resp.status_code == 400
        assert "too long" in resp.json()["detail"].lower()

    async def test_patch_nonexistent_asset(self, client):
        resp = await client.patch(
            f"/api/assets/{uuid.uuid4()}",
            json={"display_name": "Ghost"},
        )
        assert resp.status_code == 404

    async def test_patch_strips_whitespace(self, client, db_session):
        asset = await _create_asset(db_session)
        await db_session.commit()

        resp = await client.patch(
            f"/api/assets/{asset.id}",
            json={"display_name": "  padded  "},
        )
        assert resp.status_code == 200
        assert resp.json()["display_name"] == "padded"

    async def test_patch_whitespace_only_clears(self, client, db_session):
        asset = await _create_asset(db_session, display_name="Old")
        await db_session.commit()

        resp = await client.patch(
            f"/api/assets/{asset.id}",
            json={"display_name": "   "},
        )
        assert resp.status_code == 200
        assert resp.json()["display_name"] is None


# ── _asset_display_name() priority logic ──


class TestAssetDisplayNamePriority:
    """Test display_name > original_filename > filename fallback."""

    def test_display_name_wins(self):
        a = Asset(filename="uuid.mp4", original_filename="orig.mp4",
                  display_name="Friendly", asset_type=AssetType.VIDEO,
                  size_bytes=0, checksum="")
        assert _asset_display_name(a) == "Friendly"

    def test_original_filename_fallback(self):
        a = Asset(filename="uuid.mp4", original_filename="orig.mp4",
                  display_name=None, asset_type=AssetType.VIDEO,
                  size_bytes=0, checksum="")
        assert _asset_display_name(a) == "orig.mp4"

    def test_filename_fallback(self):
        a = Asset(filename="uuid.mp4", original_filename=None,
                  display_name=None, asset_type=AssetType.VIDEO,
                  size_bytes=0, checksum="")
        assert _asset_display_name(a) == "uuid.mp4"

    def test_none_asset(self):
        assert _asset_display_name(None) == "—"

    def test_empty_display_name_falls_through(self):
        """Empty string display_name should behave like None (falsy)."""
        a = Asset(filename="uuid.mp4", original_filename="orig.mp4",
                  display_name="", asset_type=AssetType.VIDEO,
                  size_bytes=0, checksum="")
        assert _asset_display_name(a) == "orig.mp4"


# ── Display names in schedule API responses ──


@pytest.mark.asyncio
class TestScheduleApiDisplayName:
    """Schedule list/detail should use display_name in asset_filename field."""

    async def test_schedule_list_uses_display_name(self, client, db_session):
        group = await _seed_group(db_session)
        asset = await _create_asset(db_session, filename="uuid-abc.mp4",
                                    display_name="Lobby Video",
                                    original_filename="lobby.mp4")
        sched = await _seed_schedule(db_session, asset, group)
        await db_session.commit()

        resp = await client.get("/api/schedules")
        assert resp.status_code == 200
        schedules = resp.json()
        match = [s for s in schedules if s["id"] == str(sched.id)]
        assert len(match) == 1
        assert match[0]["asset_filename"] == "Lobby Video"

    async def test_schedule_list_falls_back_to_original(self, client, db_session):
        group = await _seed_group(db_session)
        asset = await _create_asset(db_session, filename="uuid-abc.mp4",
                                    display_name=None,
                                    original_filename="lobby.mp4")
        sched = await _seed_schedule(db_session, asset, group)
        await db_session.commit()

        resp = await client.get("/api/schedules")
        assert resp.status_code == 200
        schedules = resp.json()
        match = [s for s in schedules if s["id"] == str(sched.id)]
        assert match[0]["asset_filename"] == "lobby.mp4"

    async def test_schedule_detail_uses_display_name(self, client, db_session):
        group = await _seed_group(db_session)
        asset = await _create_asset(db_session, filename="uuid.mp4",
                                    display_name="Welcome Reel")
        sched = await _seed_schedule(db_session, asset, group)
        await db_session.commit()

        resp = await client.get(f"/api/schedules/{sched.id}")
        assert resp.status_code == 200
        assert resp.json()["asset_filename"] == "Welcome Reel"


# ── skipped_until DB persistence ──


@pytest.mark.asyncio
class TestSkippedUntilPersistence:
    """Test that End Now writes skipped_until to DB and _ensure_skips_loaded reads it back."""

    def setup_method(self):
        import cms.services.scheduler as _sched
        _skipped.clear()
        _now_playing.clear()
        _sched._skipped_loaded = False

    def teardown_method(self):
        import cms.services.scheduler as _sched
        _skipped.clear()
        _now_playing.clear()
        _sched._skipped_loaded = False

    async def test_end_now_persists_skipped_until(self, client, db_session):
        """POST end-now should write skipped_until to the schedule row."""
        group = await _seed_group(db_session)
        asset = await _create_asset(db_session)
        sched = await _seed_schedule(db_session, asset, group)
        await db_session.commit()

        resp = await client.post(f"/api/schedules/{sched.id}/end-now")
        assert resp.status_code == 200

        # Verify DB has skipped_until set — close and reopen session to
        # avoid identity-map cache returning stale data.
        await db_session.close()
        from sqlalchemy.ext.asyncio import AsyncSession
        async with AsyncSession(db_session.bind, expire_on_commit=False) as fresh:
            result = await fresh.execute(
                select(Schedule.skipped_until).where(Schedule.id == sched.id)
            )
            skipped_val = result.scalar_one()
            assert skipped_val is not None

    async def test_ensure_skips_loaded_reads_from_db(self, db_session):
        """_ensure_skips_loaded should populate _skipped from DB rows."""
        group = await _seed_group(db_session)
        asset = await _create_asset(db_session)
        sched = await _seed_schedule(db_session, asset, group)

        # Manually set skipped_until like End Now would
        future = datetime.now(timezone.utc) + timedelta(hours=6)
        sched.skipped_until = future
        await db_session.commit()

        # Simulate fresh start: _skipped is empty, _skipped_loaded is False
        _skipped.clear()

        await _ensure_skips_loaded(db_session)

        assert str(sched.id) in _skipped

    async def test_ensure_skips_loaded_idempotent(self, db_session):
        """Second call should not re-query or duplicate entries."""
        group = await _seed_group(db_session)
        asset = await _create_asset(db_session)
        sched = await _seed_schedule(db_session, asset, group)
        sched.skipped_until = datetime.now(timezone.utc) + timedelta(hours=6)
        await db_session.commit()

        await _ensure_skips_loaded(db_session)
        count_after_first = len(_skipped)

        await _ensure_skips_loaded(db_session)
        assert len(_skipped) == count_after_first

    async def test_schedule_edit_clears_skipped_until(self, client, db_session):
        """Editing a schedule should clear its skipped_until in DB."""
        group = await _seed_group(db_session)
        asset = await _create_asset(db_session)
        sched = await _seed_schedule(db_session, asset, group)
        await db_session.commit()

        # End it
        await client.post(f"/api/schedules/{sched.id}/end-now")

        # Now edit it (change name)
        resp = await client.patch(
            f"/api/schedules/{sched.id}",
            json={"name": "Updated Schedule"},
        )
        assert resp.status_code == 200

        # Verify DB has skipped_until cleared (use fresh select)
        result = await db_session.execute(
            select(Schedule.skipped_until).where(Schedule.id == sched.id)
        )
        skipped_val = result.scalar_one()
        assert skipped_val is None

    async def test_no_skips_loaded_when_db_empty(self, db_session):
        """When no schedules have skipped_until, _skipped stays empty."""
        await _ensure_skips_loaded(db_session)
        assert len(_skipped) == 0
