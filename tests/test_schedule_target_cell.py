"""The schedules Target cell must not spill into the Schedule column.

``th, td`` are globally ``white-space: nowrap`` and ``.table-schedules
.cell-truncate`` sets ``overflow: visible`` (so tooltips can escape the cell),
which means a cell with no truncation of its own renders its overflow on top
of the next column. The Target cell was the only cell in the row without it,
so a long tag name ran over the Schedule column.

The cell is rendered by ``_macros.schedule_target_cell`` and shared between
the active table, the expired table and the ``/api/schedules/{id}/row``
fragment endpoint, so these tests pin the contract in one place.
"""

import re
import uuid
from datetime import time
from pathlib import Path

import pytest

from cms.models.asset import Asset, AssetType
from cms.models.device import DeviceGroup
from cms.models.schedule import Schedule
from cms.services import device_tags as tag_service

TEMPLATES = Path(__file__).resolve().parents[1] / "cms" / "templates"
STYLESHEET = Path(__file__).resolve().parents[1] / "cms" / "static" / "style.css"

# Tag names are allowed up to 64 chars (cms/schemas/tag.py), against a Target
# column that is ~11% of the table.
LONG_TAG = "Seasonal Promotional Content For The Holidays"


def _read(name: str) -> str:
    return (TEMPLATES / name).read_text(encoding="utf-8")


async def _seed_tagged_schedule(db_session):
    group = DeviceGroup(id=uuid.uuid4(), name="Lobby Screens")
    asset = Asset(
        id=uuid.uuid4(),
        filename="promo.mp4",
        original_filename="promo.mp4",
        asset_type=AssetType.VIDEO,
        checksum="targetchk",
        size_bytes=10,
    )
    db_session.add_all([group, asset])
    await db_session.flush()

    tag = await tag_service.create_tag(
        db_session, group_id=group.id, name=LONG_TAG, color="#ff6600"
    )
    await db_session.flush()

    schedule = Schedule(
        id=uuid.uuid4(),
        name="Holiday Loop",
        group_id=group.id,
        tag_id=tag.id,
        asset_id=asset.id,
        start_time=time(8, 0),
        end_time=time(12, 0),
    )
    db_session.add(schedule)
    await db_session.commit()
    return schedule


# ── Rendered output ──


@pytest.mark.asyncio
class TestRenderedTargetCell:
    async def test_row_fragment_clamps_a_long_tag(self, client, db_session):
        schedule = await _seed_tagged_schedule(db_session)
        resp = await client.get(f"/api/schedules/{schedule.id}/row")
        assert resp.status_code == 200
        body = resp.text

        assert 'class="cell-truncate schedule-target"' in body, (
            "the Target cell must opt into truncation like every other cell in "
            "the row, or a long tag overflows onto the Schedule column"
        )
        assert "target-tag" in body, "the tag pill needs its own clamping class"
        assert LONG_TAG in body

    async def test_full_tag_name_survives_in_the_tooltip(self, client, db_session):
        """Clamping is visual only -- hovering must still reveal the full name."""
        schedule = await _seed_tagged_schedule(db_session)
        body = (await client.get(f"/api/schedules/{schedule.id}/row")).text

        titles = re.findall(r'title="([^"]*)"', body)
        assert any(LONG_TAG in t for t in titles), (
            f"no title attribute carries the full tag name; found {titles!r}"
        )
        assert any("Lobby Screens" == t for t in titles), (
            "the group name needs its own title too -- it clamps independently"
        )

    async def test_group_without_a_tag_renders_no_pill(self, client, db_session):
        group = DeviceGroup(id=uuid.uuid4(), name="Untagged Group")
        asset = Asset(
            id=uuid.uuid4(),
            filename="plain.mp4",
            original_filename="plain.mp4",
            asset_type=AssetType.VIDEO,
            checksum="plainchk",
            size_bytes=10,
        )
        db_session.add_all([group, asset])
        await db_session.flush()
        schedule = Schedule(
            id=uuid.uuid4(),
            name="Plain Loop",
            group_id=group.id,
            asset_id=asset.id,
            start_time=time(8, 0),
            end_time=time(12, 0),
        )
        db_session.add(schedule)
        await db_session.commit()

        body = (await client.get(f"/api/schedules/{schedule.id}/row")).text
        assert "Untagged Group" in body
        assert "target-tag" not in body


# ── Source-level contract ──


def test_both_tables_use_the_shared_target_macro():
    """Active and expired tables must not drift apart again."""
    macros = _read("_macros.html")
    schedules = _read("schedules.html")

    assert "{% macro schedule_target_cell(s) -%}" in macros
    assert "{{ schedule_target_cell(s) }}" in macros, (
        "active_schedule_row must call the shared macro"
    )
    assert "{{ macros.schedule_target_cell(s) }}" in schedules, (
        "the expired-schedules table must call the shared macro rather than "
        "hand-rolling its own Target cell"
    )


def test_target_tag_pill_is_clamped_by_css():
    css = STYLESHEET.read_text(encoding="utf-8")
    match = re.search(
        r"\.table-schedules \.schedule-target \.target-tag \{(.*?)\}", css, re.S
    )
    assert match, "no CSS rule clamps the schedule Target tag pill"
    rule = match.group(1)
    for prop in ("overflow: hidden", "text-overflow: ellipsis", "max-width: 100%"):
        assert prop in rule, f"the .target-tag rule is missing {prop!r}"
