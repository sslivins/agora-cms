"""Regression test for the schedules page expired/active partition.

The /schedules page renders schedules into two tables based on end_date:
  - Active table:  <tbody id="active-schedules-tbody">
  - Expired table: inside <table class="table-schedules table-expired">

The client-side poller seeds row signatures only from the active tbody
(see cms/templates/schedules.html). If an expired schedule were
mis-classified into the active tbody, the JS would attempt a per-row
fragment swap on its next poll. The fragment endpoint always emits
active-row markup (7 cells with an Enabled toggle), but the expired
table has only 6 columns — so swapping there pushes the Actions cell
outside the panel and grafts a phantom Enabled button onto the row.

This test pins the partition contract so a future refactor can't
regress to that state silently.
"""

from __future__ import annotations

import io
import re
import uuid
from datetime import datetime, time, timedelta, timezone

import pytest


def _upload(filename: str, content: bytes = b"x"):
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
    name: str,
    end_date: datetime | None,
) -> uuid.UUID:
    from cms.models.schedule import Schedule

    sched = Schedule(
        id=uuid.uuid4(),
        name=name,
        asset_id=asset_id,
        group_id=group_id,
        start_time=time(0, 0),
        end_time=time(23, 59),
        enabled=True,
        end_date=end_date,
    )
    db_session.add(sched)
    await db_session.flush()
    return sched.id


def _row_in_active_tbody(html: str, schedule_id: uuid.UUID) -> bool:
    """True iff a <tr data-schedule-id="..."> for this id appears inside
    the <tbody id="active-schedules-tbody"> ... </tbody> block."""
    m = re.search(
        r'<tbody id="active-schedules-tbody">(.*?)</tbody>',
        html,
        re.DOTALL,
    )
    if not m:
        return False
    return f'data-schedule-id="{schedule_id}"' in m.group(1)


def _row_in_expired_table(html: str, schedule_id: uuid.UUID) -> bool:
    """True iff a <tr data-schedule-id="..."> for this id appears inside
    the expired schedules <table class="table-schedules table-expired">."""
    m = re.search(
        r'<table class="table-schedules table-expired">(.*?)</table>',
        html,
        re.DOTALL,
    )
    if not m:
        return False
    return f'data-schedule-id="{schedule_id}"' in m.group(1)


@pytest.mark.asyncio
class TestSchedulesPageExpiredPartition:
    async def test_expired_schedule_only_in_expired_table(
        self, client, db_session
    ):
        """A schedule whose end_date is in the past must render in the
        expired table, NOT the active tbody."""
        up = await client.post("/api/assets/upload", files=_upload("e.mp4"))
        asset_id = uuid.UUID(up.json()["id"])
        group_id = await _make_group(db_session)

        past = datetime.now(timezone.utc) - timedelta(days=3)
        sid = await _make_schedule(
            db_session,
            asset_id=asset_id,
            group_id=group_id,
            name=f"expired-{uuid.uuid4().hex[:6]}",
            end_date=past,
        )
        await db_session.commit()

        resp = await client.get("/schedules")
        assert resp.status_code == 200
        html = resp.text

        assert _row_in_expired_table(html, sid), (
            "Expired schedule should render in the expired table"
        )
        assert not _row_in_active_tbody(html, sid), (
            "Expired schedule must NOT appear in #active-schedules-tbody — "
            "the JS poller would swap its row with active-shaped (7-cell) "
            "markup on next poll, breaking the 6-column expired table layout."
        )

    async def test_active_schedule_only_in_active_tbody(
        self, client, db_session
    ):
        """A schedule with no end_date (or future end_date) must render
        in the active tbody, NOT the expired table."""
        up = await client.post("/api/assets/upload", files=_upload("a.mp4"))
        asset_id = uuid.UUID(up.json()["id"])
        group_id = await _make_group(db_session)

        future = datetime.now(timezone.utc) + timedelta(days=30)
        sid = await _make_schedule(
            db_session,
            asset_id=asset_id,
            group_id=group_id,
            name=f"active-{uuid.uuid4().hex[:6]}",
            end_date=future,
        )
        await db_session.commit()

        resp = await client.get("/schedules")
        assert resp.status_code == 200
        html = resp.text

        assert _row_in_active_tbody(html, sid)
        assert not _row_in_expired_table(html, sid)

    async def test_expired_table_has_six_column_colgroup(
        self, client, db_session
    ):
        """The expired table's colgroup must have exactly 6 cols. The
        active table has 7 (the extra is the Enabled column). This pins
        the structural difference the JS poller relies on."""
        up = await client.post("/api/assets/upload", files=_upload("c.mp4"))
        asset_id = uuid.UUID(up.json()["id"])
        group_id = await _make_group(db_session)

        past = datetime.now(timezone.utc) - timedelta(days=1)
        await _make_schedule(
            db_session,
            asset_id=asset_id,
            group_id=group_id,
            name=f"colcheck-{uuid.uuid4().hex[:6]}",
            end_date=past,
        )
        await db_session.commit()

        resp = await client.get("/schedules")
        assert resp.status_code == 200
        html = resp.text

        m = re.search(
            r'<table class="table-schedules table-expired">.*?<colgroup>(.*?)</colgroup>',
            html,
            re.DOTALL,
        )
        assert m, "expired table missing colgroup"
        cols = re.findall(r"<col\b", m.group(1))
        assert len(cols) == 6, (
            f"expired colgroup must have 6 <col> entries (got {len(cols)}); "
            "if you change the expired table shape, update the JS poller "
            "in cms/templates/schedules.html so it doesn't swap rows whose "
            "shape doesn't match."
        )
