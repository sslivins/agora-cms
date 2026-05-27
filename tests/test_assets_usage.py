"""Tests for the asset "Used in N" badge + usage filter + schedule
activation guard against soft-deleted assets.
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

    g = DeviceGroup(id=uuid.uuid4(), name=f"grp-{uuid.uuid4().hex[:8]}")
    db_session.add(g)
    await db_session.flush()
    return g.id


async def _make_schedule(
    db_session,
    *,
    asset_id: uuid.UUID,
    group_id: uuid.UUID,
    name: str | None = None,
    enabled: bool = True,
    end_date: datetime | None = None,
) -> uuid.UUID:
    from cms.models.schedule import Schedule

    sched = Schedule(
        id=uuid.uuid4(),
        name=name or f"sched-{uuid.uuid4().hex[:8]}",
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


async def _upload(client, name: str) -> uuid.UUID:
    resp = await client.post("/api/assets/upload", files=_make_upload(name))
    assert resp.status_code in (200, 201), resp.text
    return uuid.UUID(resp.json()["id"])


@pytest.mark.asyncio
class TestAssetUsage:
    async def test_unused_asset_has_zero_total(self, client):
        aid = await _upload(client, "lonely.mp4")
        resp = await client.get("/api/assets/page")
        assert resp.status_code == 200
        match = next(it for it in resp.json()["items"] if it["id"] == str(aid))
        assert match["usage"]["total"] == 0
        assert match["usage"]["schedules"] == []
        assert match["usage"]["slides"] == []

    async def test_disabled_schedule_counts_as_used(self, client, db_session):
        aid = await _upload(client, "disabled.mp4")
        gid = await _make_group(db_session)
        await _make_schedule(
            db_session, asset_id=aid, group_id=gid,
            name="MyDisabledSched", enabled=False,
        )
        await db_session.commit()

        resp = await client.get("/api/assets/page")
        match = next(it for it in resp.json()["items"] if it["id"] == str(aid))
        assert match["usage"]["total"] == 1
        names = [s["name"] for s in match["usage"]["schedules"]]
        assert "MyDisabledSched" in names

    async def test_expired_schedule_does_not_count(self, client, db_session):
        aid = await _upload(client, "expired-only.mp4")
        gid = await _make_group(db_session)
        past = datetime.now(timezone.utc) - timedelta(days=1)
        await _make_schedule(
            db_session, asset_id=aid, group_id=gid, end_date=past
        )
        await db_session.commit()

        resp = await client.get("/api/assets/page")
        match = next(it for it in resp.json()["items"] if it["id"] == str(aid))
        assert match["usage"]["total"] == 0

    async def test_usage_filter_used_and_unused(self, client, db_session):
        used_id = await _upload(client, "used.mp4")
        unused_id = await _upload(client, "unused.mp4")
        gid = await _make_group(db_session)
        await _make_schedule(db_session, asset_id=used_id, group_id=gid)
        await db_session.commit()

        used_resp = await client.get("/api/assets/page?usage=used")
        used_ids = {it["id"] for it in used_resp.json()["items"]}
        assert str(used_id) in used_ids
        assert str(unused_id) not in used_ids

        unused_resp = await client.get("/api/assets/page?usage=unused")
        unused_ids = {it["id"] for it in unused_resp.json()["items"]}
        assert str(unused_id) in unused_ids
        assert str(used_id) not in unused_ids

    async def test_usage_filter_rejects_bad_value(self, client):
        resp = await client.get("/api/assets/page?usage=bogus")
        assert resp.status_code == 400


@pytest.mark.asyncio
class TestScheduleActivationGuard:
    async def test_create_schedule_against_deleted_asset_is_rejected(
        self, client, db_session
    ):
        """POST /api/schedules with a soft-deleted asset returns 409."""
        from cms.models.asset import Asset

        aid = await _upload(client, "deleted-target.mp4")
        # Soft-delete it directly so we can independently exercise the guard
        # (the API delete-block now prevents this for non-expired schedules,
        # but here we're simulating an asset that was deleted while no
        # schedules referenced it).
        a = await db_session.get(Asset, aid)
        a.deleted_at = datetime.now(timezone.utc)
        await db_session.commit()

        gid = await _make_group(db_session)
        resp = await client.post("/api/schedules", json={
            "name": f"sched-{uuid.uuid4().hex[:6]}",
            "asset_id": str(aid),
            "group_id": str(gid),
            "start_time": "00:00:00",
            "end_time": "23:59:00",
            "enabled": True,
        })
        assert resp.status_code == 409
        assert "deleted" in resp.json()["detail"].lower()

    async def test_patch_enable_against_deleted_asset_is_rejected(
        self, client, db_session
    ):
        """A disabled schedule whose asset later gets soft-deleted cannot
        be flipped back to enabled."""
        from cms.models.asset import Asset

        aid = await _upload(client, "patch-target.mp4")
        gid = await _make_group(db_session)
        sched_id = await _make_schedule(
            db_session, asset_id=aid, group_id=gid, enabled=False
        )
        a = await db_session.get(Asset, aid)
        a.deleted_at = datetime.now(timezone.utc)
        await db_session.commit()

        resp = await client.patch(f"/api/schedules/{sched_id}", json={
            "enabled": True,
        })
        assert resp.status_code == 409
        assert "deleted" in resp.json()["detail"].lower()
