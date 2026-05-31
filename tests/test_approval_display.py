"""Tests for the approval-card friendly-name resolver.

See ``cms/services/assistant/approval_display.py``.
"""

import uuid

import pytest

from cms.models.asset import Asset
from cms.models.device import Device, DeviceGroup, DeviceStatus
from cms.services.assistant.approval_display import resolve_friendly_names


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _device(db, did="pi-100", name="Pi100", group_id=None):
    d = Device(id=did, name=name, status=DeviceStatus.ADOPTED, group_id=group_id)
    db.add(d)
    await db.commit()
    return d


async def _group(db, name="Lobby"):
    g = DeviceGroup(name=name, description="")
    db.add(g)
    await db.commit()
    await db.refresh(g)
    return g


async def _asset(db, **fields):
    defaults = dict(
        id=uuid.uuid4(),
        filename="x.png",
        original_filename="x.png",
        asset_type="image",
        size_bytes=10,
    )
    defaults.update(fields)
    a = Asset(**defaults)
    db.add(a)
    await db.commit()
    await db.refresh(a)
    return a


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.asyncio


class TestSingleResolution:
    async def test_device_resolves(self, db_session):
        await _device(db_session, did="pi-100", name="Pi100")
        out = await resolve_friendly_names(db_session, {"device_id": "pi-100"})
        assert out == {"device_id": "Pi100"}

    async def test_group_resolves(self, db_session):
        g = await _group(db_session, name="Lobby")
        out = await resolve_friendly_names(db_session, {"group_id": str(g.id)})
        assert out == {"group_id": "Lobby"}

    async def test_asset_display_name_wins(self, db_session):
        a = await _asset(db_session, display_name="Promo", original_filename="raw.mp4", filename="abc.mp4")
        out = await resolve_friendly_names(db_session, {"default_asset_id": str(a.id)})
        assert out == {"default_asset_id": "Promo"}

    async def test_asset_falls_back_to_original_filename(self, db_session):
        a = await _asset(db_session, display_name=None, original_filename="raw.mp4", filename="abc.mp4")
        out = await resolve_friendly_names(db_session, {"asset_id": str(a.id)})
        assert out == {"asset_id": "raw.mp4"}

    async def test_asset_falls_back_to_filename(self, db_session):
        a = await _asset(db_session, display_name=None, original_filename=None, filename="abc.mp4")
        out = await resolve_friendly_names(db_session, {"asset_id": str(a.id)})
        assert out == {"asset_id": "abc.mp4"}


class TestPluralResolution:
    async def test_device_list_resolves(self, db_session):
        await _device(db_session, did="pi-a", name="Lobby-A")
        await _device(db_session, did="pi-b", name="Lobby-B")
        out = await resolve_friendly_names(
            db_session, {"device_ids": ["pi-a", "pi-b"]}
        )
        assert out == {"device_ids": ["Lobby-A", "Lobby-B"]}

    async def test_plural_with_partial_miss(self, db_session):
        await _device(db_session, did="pi-a", name="Lobby-A")
        out = await resolve_friendly_names(
            db_session, {"device_ids": ["pi-a", "pi-missing"]}
        )
        assert out == {"device_ids": ["Lobby-A", None]}

    async def test_plural_all_miss_skipped(self, db_session):
        out = await resolve_friendly_names(
            db_session, {"device_ids": ["pi-missing-1", "pi-missing-2"]}
        )
        assert out == {}


class TestSkipping:
    async def test_unknown_key_ignored(self, db_session):
        out = await resolve_friendly_names(
            db_session, {"random_field": "whatever", "other": 42}
        )
        assert out == {}

    async def test_missing_row_skipped(self, db_session):
        out = await resolve_friendly_names(
            db_session, {"device_id": "does-not-exist"}
        )
        assert out == {}

    async def test_malformed_input_skipped(self, db_session):
        out = await resolve_friendly_names(
            db_session, {"device_id": 12345, "group_id": ["not", "a", "uuid"]}
        )
        assert out == {}

    async def test_empty_input(self, db_session):
        assert await resolve_friendly_names(db_session, {}) == {}
        assert await resolve_friendly_names(db_session, None) == {}  # type: ignore[arg-type]

    async def test_mixed_resolves_some_skips_others(self, db_session):
        await _device(db_session, did="pi-100", name="Pi100")
        out = await resolve_friendly_names(
            db_session,
            {
                "device_id": "pi-100",
                "asset_id": "not-real-uuid-but-string",
                "junk": "ignored",
            },
        )
        # device resolves; asset_id has no matching row → skipped.
        assert out == {"device_id": "Pi100"}
