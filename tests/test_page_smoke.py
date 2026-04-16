"""Smoke tests: every UI page must return 200 with data in the DB.

Seeds a device, group, asset, and schedule so every page exercises real
ORM model loading (enum deserialization, relationships, etc.).  Would
have caught the SAVED_STREAM enum crash on the assets page.
"""

import uuid
from datetime import time

import pytest
import pytest_asyncio

from cms.models.device import Device, DeviceGroup, DeviceStatus
from shared.models.asset import Asset, AssetType
from cms.models.schedule import Schedule

# All authenticated HTML pages a user can navigate to.
UI_PAGES = [
    "/",
    "/devices",
    "/assets",
    "/schedules",
    "/profiles",
    "/settings",
    "/history",
    "/users",
    "/profile",
]


@pytest_asyncio.fixture
async def seeded_db(db_session):
    """Seed one of each core object so pages have data to render."""
    group = DeviceGroup(id=uuid.uuid4(), name="Smoke Group")
    db_session.add(group)
    await db_session.flush()

    device = Device(
        id="smoke-pi-001", name="Smoke Device",
        status=DeviceStatus.ADOPTED, group_id=group.id,
    )
    asset = Asset(
        id=uuid.uuid4(), filename="smoke-video.mp4",
        asset_type=AssetType.VIDEO, size_bytes=1024, checksum="abc",
    )
    db_session.add_all([device, asset])
    await db_session.flush()

    schedule = Schedule(
        id=uuid.uuid4(), name="Smoke Schedule",
        group_id=group.id, asset_id=asset.id,
        start_time=time(8, 0), end_time=time(17, 0),
    )
    db_session.add(schedule)
    await db_session.commit()


@pytest.mark.asyncio
class TestPageSmoke:
    """Every UI page must return 200 when the DB has data."""

    @pytest.mark.parametrize("path", UI_PAGES)
    async def test_page_returns_200(self, client, seeded_db, path):
        resp = await client.get(path, follow_redirects=True)
        assert resp.status_code == 200, (
            f"GET {path} returned {resp.status_code}"
        )
