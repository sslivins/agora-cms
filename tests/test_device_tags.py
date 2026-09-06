"""Group-scoped device tags.

Tags replace many-to-many device↔group authority (#863): they let one device
serve several purposes without introducing a second access-control boundary.
The properties that make that safe are what these tests pin down:

* a tag belongs to exactly one group, so ``Group A:Summer`` and
  ``Group B:Summer`` are unrelated and no target can span both;
* a schedule may only be narrowed by a tag from the group it targets;
* two same-group schedules at equal priority overlapping in time conflict
  unless they are narrowed to tags with no device in common;
* tagging a device cannot smuggle in a conflict the schedule gate refused;
* a device that leaves its group leaves that group's tags behind.
"""

import uuid
from datetime import time

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from cms.models.asset import Asset, AssetType
from cms.models.device import Device, DeviceGroup, DeviceStatus
from cms.models.device_tag import DeviceTag, DeviceTagAssignment
from cms.models.schedule import Schedule
from cms.services import device_tags as tag_service
from cms.services.device_membership import set_device_group


async def _seed(db_session):
    group_a = DeviceGroup(id=uuid.uuid4(), name="Tag Group A")
    group_b = DeviceGroup(id=uuid.uuid4(), name="Tag Group B")
    asset = Asset(
        id=uuid.uuid4(),
        filename="tagged.mp4",
        original_filename="tagged.mp4",
        asset_type=AssetType.VIDEO,
        checksum="tagchk",
        size_bytes=10,
    )
    db_session.add_all([group_a, group_b, asset])
    await db_session.flush()
    return group_a, group_b, asset


async def _device(db_session, device_id, group_id):
    dev = Device(id=device_id, name=device_id, status=DeviceStatus.ADOPTED)
    db_session.add(dev)
    await db_session.flush()
    dev.group_id = group_id
    await db_session.flush()
    return dev


@pytest.mark.asyncio
class TestTagScoping:
    async def test_same_name_in_two_groups_are_distinct_tags(self, db_session):
        group_a, group_b, _ = await _seed(db_session)
        tag_a = await tag_service.create_tag(
            db_session, group_id=group_a.id, name="Summer Promos"
        )
        tag_b = await tag_service.create_tag(
            db_session, group_id=group_b.id, name="Summer Promos"
        )
        await db_session.commit()
        assert tag_a.id != tag_b.id
        assert tag_a.group_id != tag_b.group_id

    async def test_duplicate_name_within_a_group_is_rejected(self, db_session):
        group_a, _, _ = await _seed(db_session)
        await tag_service.create_tag(db_session, group_id=group_a.id, name="Lobby")
        await db_session.commit()
        with pytest.raises(tag_service.DeviceTagError):
            await tag_service.create_tag(
                db_session, group_id=group_a.id, name="  lobby  "
            )

    async def test_device_cannot_take_a_tag_from_another_group(self, db_session):
        group_a, group_b, _ = await _seed(db_session)
        foreign = await tag_service.create_tag(
            db_session, group_id=group_b.id, name="Foreign"
        )
        dev = await _device(db_session, "tag-dev-1", group_a.id)
        await db_session.commit()
        with pytest.raises(tag_service.DeviceTagError):
            await tag_service.set_device_tags(db_session, dev, [foreign.id])

    async def test_moving_group_drops_tags(self, db_session):
        group_a, group_b, _ = await _seed(db_session)
        tag = await tag_service.create_tag(
            db_session, group_id=group_a.id, name="Kept In A"
        )
        dev = await _device(db_session, "tag-dev-2", group_a.id)
        await tag_service.set_device_tags(db_session, dev, [tag.id])
        await db_session.commit()

        change = await set_device_group(db_session, dev, group_b.id)
        await db_session.commit()

        assert change.dropped_tags == ["Kept In A"]
        remaining = await db_session.execute(
            select(DeviceTagAssignment).where(
                DeviceTagAssignment.device_id == dev.id
            )
        )
        assert remaining.scalars().all() == []

    async def test_deleting_a_group_deletes_its_tags(self, db_session):
        group_a, _, _ = await _seed(db_session)
        await tag_service.create_tag(db_session, group_id=group_a.id, name="Doomed")
        await db_session.commit()
        await db_session.delete(group_a)
        await db_session.commit()
        rows = await db_session.execute(
            select(DeviceTag).where(DeviceTag.group_id == group_a.id)
        )
        assert rows.scalars().all() == []


@pytest.mark.asyncio
class TestScheduleTargeting:
    """A schedule's target is ``<Group>`` or ``<Group>:<Tag>`` — never wider."""

    async def _client(self, app):
        return AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        )

    async def _payload(self, group_id, asset_id, **over):
        payload = {
            "name": over.pop("name", "Tagged schedule"),
            "group_id": str(group_id),
            "asset_id": str(asset_id),
            "start_time": "09:00:00",
            "end_time": "17:00:00",
            "priority": 5,
            "enabled": True,
        }
        payload.update({k: v for k, v in over.items()})
        return payload

    async def test_tag_from_another_group_is_rejected(
        self, app, client, db_session
    ):
        group_a, group_b, asset = await _seed(db_session)
        foreign = await tag_service.create_tag(
            db_session, group_id=group_b.id, name="Elsewhere"
        )
        await db_session.commit()

        resp = await client.post(
            "/api/schedules",
            json=await self._payload(
                group_a.id, asset.id, tag_id=str(foreign.id)
            ),
        )
        assert resp.status_code == 422, resp.text
        assert "different group" in resp.json()["detail"]

    async def test_overlapping_tag_subsets_conflict(self, client, db_session):
        group_a, _, asset = await _seed(db_session)
        shared = await _device(db_session, "tag-dev-shared", group_a.id)
        morning = await tag_service.create_tag(
            db_session, group_id=group_a.id, name="Morning"
        )
        evening = await tag_service.create_tag(
            db_session, group_id=group_a.id, name="Evening"
        )
        # The same device carries both tags, so both schedules reach it.
        await tag_service.set_device_tags(
            db_session, shared, [morning.id, evening.id]
        )
        await db_session.commit()

        first = await client.post(
            "/api/schedules",
            json=await self._payload(
                group_a.id, asset.id, name="Morning show", tag_id=str(morning.id)
            ),
        )
        assert first.status_code == 201, first.text

        second = await client.post(
            "/api/schedules",
            json=await self._payload(
                group_a.id, asset.id, name="Evening show", tag_id=str(evening.id)
            ),
        )
        assert second.status_code == 409, second.text

    async def test_disjoint_tag_subsets_do_not_conflict(self, client, db_session):
        group_a, _, asset = await _seed(db_session)
        lobby_dev = await _device(db_session, "tag-dev-lobby", group_a.id)
        cafe_dev = await _device(db_session, "tag-dev-cafe", group_a.id)
        lobby = await tag_service.create_tag(
            db_session, group_id=group_a.id, name="Lobby"
        )
        cafe = await tag_service.create_tag(
            db_session, group_id=group_a.id, name="Cafe"
        )
        await tag_service.set_device_tags(db_session, lobby_dev, [lobby.id])
        await tag_service.set_device_tags(db_session, cafe_dev, [cafe.id])
        await db_session.commit()

        first = await client.post(
            "/api/schedules",
            json=await self._payload(
                group_a.id, asset.id, name="Lobby loop", tag_id=str(lobby.id)
            ),
        )
        assert first.status_code == 201, first.text

        second = await client.post(
            "/api/schedules",
            json=await self._payload(
                group_a.id, asset.id, name="Cafe loop", tag_id=str(cafe.id)
            ),
        )
        assert second.status_code == 201, second.text

    async def test_untagged_schedule_still_conflicts_with_a_tagged_one(
        self, client, db_session
    ):
        group_a, _, asset = await _seed(db_session)
        dev = await _device(db_session, "tag-dev-any", group_a.id)
        lobby = await tag_service.create_tag(
            db_session, group_id=group_a.id, name="Lobby"
        )
        await tag_service.set_device_tags(db_session, dev, [lobby.id])
        await db_session.commit()

        first = await client.post(
            "/api/schedules",
            json=await self._payload(
                group_a.id, asset.id, name="Lobby only", tag_id=str(lobby.id)
            ),
        )
        assert first.status_code == 201, first.text

        # No tag = the whole group, which necessarily includes the lobby subset.
        second = await client.post(
            "/api/schedules",
            json=await self._payload(group_a.id, asset.id, name="Whole group"),
        )
        assert second.status_code == 409, second.text

    async def test_tagged_schedule_only_syncs_to_tagged_devices(self, db_session):
        from cms.services.scheduler import load_target_devices_by_schedule

        group_a, _, asset = await _seed(db_session)
        tagged_dev = await _device(db_session, "tag-dev-in", group_a.id)
        await _device(db_session, "tag-dev-out", group_a.id)
        lobby = await tag_service.create_tag(
            db_session, group_id=group_a.id, name="Lobby"
        )
        await tag_service.set_device_tags(db_session, tagged_dev, [lobby.id])
        schedule = Schedule(
            id=uuid.uuid4(),
            name="Lobby only",
            group_id=group_a.id,
            tag_id=lobby.id,
            asset_id=asset.id,
            start_time=time(9, 0),
            end_time=time(17, 0),
            priority=5,
            enabled=True,
        )
        db_session.add(schedule)
        await db_session.commit()

        targets = await load_target_devices_by_schedule([schedule], db_session)
        assert targets[str(schedule.id)] == {"tag-dev-in"}


@pytest.mark.asyncio
class TestTaggingGate:
    """Tagging must not create a conflict the schedule gate would have refused."""

    async def test_tagging_a_device_into_an_overlap_is_rejected(
        self, client, db_session
    ):
        group_a, _, asset = await _seed(db_session)
        lobby_dev = await _device(db_session, "gate-dev-lobby", group_a.id)
        cafe_dev = await _device(db_session, "gate-dev-cafe", group_a.id)
        lobby = await tag_service.create_tag(
            db_session, group_id=group_a.id, name="Lobby"
        )
        cafe = await tag_service.create_tag(
            db_session, group_id=group_a.id, name="Cafe"
        )
        await tag_service.set_device_tags(db_session, lobby_dev, [lobby.id])
        await tag_service.set_device_tags(db_session, cafe_dev, [cafe.id])
        db_session.add_all([
            Schedule(
                id=uuid.uuid4(),
                name="Lobby loop",
                group_id=group_a.id,
                tag_id=lobby.id,
                asset_id=asset.id,
                start_time=time(9, 0),
                end_time=time(17, 0),
                priority=5,
                enabled=True,
            ),
            Schedule(
                id=uuid.uuid4(),
                name="Cafe loop",
                group_id=group_a.id,
                tag_id=cafe.id,
                asset_id=asset.id,
                start_time=time(9, 0),
                end_time=time(17, 0),
                priority=5,
                enabled=True,
            ),
        ])
        await db_session.commit()

        # Both schedules were legal because their subsets were disjoint. Adding
        # the second tag to the lobby device would make them collide on it.
        resp = await client.put(
            f"/api/devices/{lobby_dev.id}/tags",
            json={"tag_ids": [str(lobby.id), str(cafe.id)]},
        )
        assert resp.status_code == 409, resp.text
        assert "Lobby loop" in resp.json()["detail"]

    async def test_harmless_tagging_is_allowed(self, client, db_session):
        group_a, _, asset = await _seed(db_session)
        dev = await _device(db_session, "gate-dev-ok", group_a.id)
        lobby = await tag_service.create_tag(
            db_session, group_id=group_a.id, name="Lobby"
        )
        await db_session.commit()

        resp = await client.put(
            f"/api/devices/{dev.id}/tags", json={"tag_ids": [str(lobby.id)]}
        )
        assert resp.status_code == 200, resp.text
        assert [t["name"] for t in resp.json()["tags"]] == ["Lobby"]
        assert resp.json()["tags"][0]["qualified_name"] == "Tag Group A:Lobby"
