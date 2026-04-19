"""Tests for deleting assets that are referenced only by expired or
disabled schedules (issue #177).

Prior behavior: the DELETE /{asset_id} endpoint blocked the delete with a
409 if *any* schedule referenced the asset, even if every referencing
schedule had already expired or been disabled.

New behavior:
 - Active schedules (enabled AND (end_date is NULL OR end_date >= now))
   still block deletion with 409.
 - Expired or disabled schedules are silently removed alongside the asset.
 - Mixed: any active schedule present → 409; once all active refs are
   removed the asset becomes deletable.
"""

from __future__ import annotations

import io
import uuid
from datetime import datetime, timedelta, time, timezone

import pytest


def _make_upload(filename: str, content: bytes = b"fakecontent"):
    return {"file": (filename, io.BytesIO(content), "application/octet-stream")}


async def _make_group(db_session) -> uuid.UUID:
    from cms.models.device import DeviceGroup

    group = DeviceGroup(id=uuid.uuid4(), name=f"grp-{uuid.uuid4().hex[:8]}")
    db_session.add(group)
    await db_session.flush()
    return group.id


async def _make_schedule(
    db_session,
    *,
    asset_id: uuid.UUID,
    group_id: uuid.UUID,
    enabled: bool = True,
    end_date: datetime | None = None,
) -> uuid.UUID:
    from cms.models.schedule import Schedule

    sched = Schedule(
        id=uuid.uuid4(),
        name=f"sched-{uuid.uuid4().hex[:8]}",
        asset_id=asset_id,
        group_id=group_id,
        start_time=time(0, 0),
        end_time=time(23, 59),
        enabled=enabled,
        end_date=end_date,
    )
    db_session.add(sched)
    await db_session.flush()
    return sched.id


@pytest.mark.asyncio
class TestAssetDeleteWithExpiredSchedules:
    async def test_delete_blocked_by_active_schedule(self, client, db_session):
        """Active schedule (enabled, no end_date) still blocks delete."""
        upload = await client.post("/api/assets/upload", files=_make_upload("active.mp4"))
        asset_id = uuid.UUID(upload.json()["id"])
        group_id = await _make_group(db_session)
        await _make_schedule(db_session, asset_id=asset_id, group_id=group_id)
        await db_session.commit()

        resp = await client.delete(f"/api/assets/{asset_id}")
        assert resp.status_code == 409
        assert "active schedule" in resp.json()["detail"].lower()

    async def test_delete_blocked_by_future_schedule(self, client, db_session):
        """Schedule whose end_date is still in the future also blocks delete."""
        upload = await client.post("/api/assets/upload", files=_make_upload("future.mp4"))
        asset_id = uuid.UUID(upload.json()["id"])
        group_id = await _make_group(db_session)
        future = datetime.now(timezone.utc) + timedelta(days=7)
        await _make_schedule(
            db_session, asset_id=asset_id, group_id=group_id, end_date=future
        )
        await db_session.commit()

        resp = await client.delete(f"/api/assets/{asset_id}")
        assert resp.status_code == 409

    async def test_delete_allowed_when_only_expired_schedules(self, client, db_session):
        """Asset referenced only by past-end_date schedules is deletable, and
        those schedule rows are removed alongside the asset."""
        from cms.models.schedule import Schedule
        from sqlalchemy import select

        upload = await client.post("/api/assets/upload", files=_make_upload("expired.mp4"))
        asset_id = uuid.UUID(upload.json()["id"])
        group_id = await _make_group(db_session)
        past = datetime.now(timezone.utc) - timedelta(days=30)
        sched_id = await _make_schedule(
            db_session, asset_id=asset_id, group_id=group_id, end_date=past
        )
        await db_session.commit()

        resp = await client.delete(f"/api/assets/{asset_id}")
        assert resp.status_code == 200

        # Expired schedule row should have been removed.
        remaining = (await db_session.execute(
            select(Schedule).where(Schedule.id == sched_id)
        )).scalar_one_or_none()
        assert remaining is None

    async def test_delete_allowed_when_only_disabled_schedules(self, client, db_session):
        """Asset referenced only by disabled schedules is deletable."""
        from cms.models.schedule import Schedule
        from sqlalchemy import select

        upload = await client.post("/api/assets/upload", files=_make_upload("disabled.mp4"))
        asset_id = uuid.UUID(upload.json()["id"])
        group_id = await _make_group(db_session)
        sched_id = await _make_schedule(
            db_session, asset_id=asset_id, group_id=group_id, enabled=False
        )
        await db_session.commit()

        resp = await client.delete(f"/api/assets/{asset_id}")
        assert resp.status_code == 200

        remaining = (await db_session.execute(
            select(Schedule).where(Schedule.id == sched_id)
        )).scalar_one_or_none()
        assert remaining is None

    async def test_delete_blocked_by_mix_with_active_schedule(self, client, db_session):
        """Mix of expired + one active: still blocked; nothing removed."""
        from cms.models.schedule import Schedule
        from sqlalchemy import select, func as _func

        upload = await client.post("/api/assets/upload", files=_make_upload("mix.mp4"))
        asset_id = uuid.UUID(upload.json()["id"])
        group_id = await _make_group(db_session)
        past = datetime.now(timezone.utc) - timedelta(days=30)
        await _make_schedule(db_session, asset_id=asset_id, group_id=group_id, end_date=past)
        await _make_schedule(db_session, asset_id=asset_id, group_id=group_id)  # active
        await db_session.commit()

        resp = await client.delete(f"/api/assets/{asset_id}")
        assert resp.status_code == 409

        # Neither schedule row should have been touched — the 409 must be
        # atomic with no partial state changes.
        count = await db_session.scalar(
            select(_func.count()).select_from(Schedule).where(Schedule.asset_id == asset_id)
        )
        assert count == 2
