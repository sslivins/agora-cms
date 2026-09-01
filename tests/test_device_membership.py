"""Tests for the device↔group membership mirror (Stage 2, #863).

Covers the dual-write helper and its wiring into the device write paths that
mutate ``group_id`` during the expand/contract coexistence window.
"""

import uuid

import pytest
from sqlalchemy import select

from cms.models.device import Device, DeviceGroup, DeviceStatus
from cms.models.device_group_membership import DeviceGroupMembership
from cms.services.device_membership import set_single_group_membership


async def _memberships(db_session, device_id):
    res = await db_session.execute(
        select(DeviceGroupMembership.group_id).where(
            DeviceGroupMembership.device_id == device_id
        )
    )
    return sorted(str(g) for g in res.scalars().all())


@pytest.mark.asyncio
class TestSetSingleGroupMembership:
    async def _device_and_groups(self, db_session):
        g1 = DeviceGroup(name="M1")
        g2 = DeviceGroup(name="M2")
        dev = Device(id="mem-pi", name="Mem", status=DeviceStatus.ADOPTED)
        db_session.add_all([g1, g2, dev])
        await db_session.commit()
        return dev.id, g1.id, g2.id

    async def test_assigns_membership(self, db_session):
        dev, g1, g2 = await self._device_and_groups(db_session)
        await set_single_group_membership(db_session, dev, g1)
        await db_session.commit()
        assert await _memberships(db_session, dev) == [str(g1)]

    async def test_replaces_membership(self, db_session):
        dev, g1, g2 = await self._device_and_groups(db_session)
        await set_single_group_membership(db_session, dev, g1)
        await db_session.commit()
        await set_single_group_membership(db_session, dev, g2)
        await db_session.commit()
        # At-most-one: the old membership is gone, only the new remains.
        assert await _memberships(db_session, dev) == [str(g2)]

    async def test_clear_membership(self, db_session):
        dev, g1, g2 = await self._device_and_groups(db_session)
        await set_single_group_membership(db_session, dev, g1)
        await db_session.commit()
        await set_single_group_membership(db_session, dev, None)
        await db_session.commit()
        assert await _memberships(db_session, dev) == []

    async def test_idempotent(self, db_session):
        dev, g1, g2 = await self._device_and_groups(db_session)
        await set_single_group_membership(db_session, dev, g1)
        await set_single_group_membership(db_session, dev, g1)
        await db_session.commit()
        assert await _memberships(db_session, dev) == [str(g1)]


@pytest.mark.asyncio
class TestDualWriteViaAPI:
    async def _device_and_groups(self, db_session):
        g1 = DeviceGroup(name="API-G1")
        g2 = DeviceGroup(name="API-G2")
        dev = Device(id="dw-pi", name="DW", status=DeviceStatus.ADOPTED)
        db_session.add_all([g1, g2, dev])
        await db_session.commit()
        return dev.id, g1.id, g2.id

    async def test_patch_group_id_mirrors_membership(self, client, db_session):
        dev, g1, g2 = await self._device_and_groups(db_session)

        resp = await client.patch(f"/api/devices/{dev}", json={"group_id": str(g1)})
        assert resp.status_code == 200
        assert await _memberships(db_session, dev) == [str(g1)]

        # Re-assigning to another group replaces the mirror.
        resp = await client.patch(f"/api/devices/{dev}", json={"group_id": str(g2)})
        assert resp.status_code == 200
        assert await _memberships(db_session, dev) == [str(g2)]

        # Clearing the group clears the mirror.
        resp = await client.patch(f"/api/devices/{dev}", json={"group_id": None})
        assert resp.status_code == 200
        assert await _memberships(db_session, dev) == []

    async def test_deleting_group_cascades_membership(self, client, db_session):
        dev, g1, g2 = await self._device_and_groups(db_session)
        await set_single_group_membership(db_session, dev, g1)
        await db_session.commit()

        # ORM-level delete-orphan / DB CASCADE both remove the join row.
        grp = await db_session.get(DeviceGroup, g1)
        await db_session.delete(grp)
        await db_session.commit()
        assert await _memberships(db_session, dev) == []
