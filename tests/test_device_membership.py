"""Tests for device group assignment.

A device belongs to exactly one group, which is its authorization boundary.
These cover the ``set_device_group`` chokepoint and its wiring into the
device write paths.
"""

import pytest
from sqlalchemy import select

from cms.models.device import Device, DeviceGroup, DeviceStatus
from cms.services.device_membership import set_device_group


async def _group_of(db_session, device_id):
    res = await db_session.execute(
        select(Device.group_id).where(Device.id == device_id)
    )
    gid = res.scalar_one()
    return str(gid) if gid else None


@pytest.mark.asyncio
class TestSetDeviceGroup:
    async def _device_and_groups(self, db_session):
        g1 = DeviceGroup(name="M1")
        g2 = DeviceGroup(name="M2")
        dev = Device(id="mem-pi", name="Mem", status=DeviceStatus.ADOPTED)
        db_session.add_all([g1, g2, dev])
        await db_session.commit()
        return dev, g1.id, g2.id

    async def test_assigns_group(self, db_session):
        dev, g1, _ = await self._device_and_groups(db_session)
        change = await set_device_group(db_session, dev, g1)
        await db_session.commit()
        assert change.changed is True
        assert change.previous_group_id is None
        assert await _group_of(db_session, dev.id) == str(g1)

    async def test_replaces_group(self, db_session):
        dev, g1, g2 = await self._device_and_groups(db_session)
        await set_device_group(db_session, dev, g1)
        await db_session.commit()
        change = await set_device_group(db_session, dev, g2)
        await db_session.commit()
        assert change.previous_group_id == g1
        assert await _group_of(db_session, dev.id) == str(g2)

    async def test_clear_group(self, db_session):
        dev, g1, _ = await self._device_and_groups(db_session)
        await set_device_group(db_session, dev, g1)
        await db_session.commit()
        await set_device_group(db_session, dev, None)
        await db_session.commit()
        assert await _group_of(db_session, dev.id) is None

    async def test_idempotent(self, db_session):
        dev, g1, _ = await self._device_and_groups(db_session)
        await set_device_group(db_session, dev, g1)
        change = await set_device_group(db_session, dev, g1)
        await db_session.commit()
        assert change.changed is False
        assert await _group_of(db_session, dev.id) == str(g1)


@pytest.mark.asyncio
class TestGroupAssignmentViaAPI:
    async def _device_and_groups(self, db_session):
        g1 = DeviceGroup(name="API-G1")
        g2 = DeviceGroup(name="API-G2")
        dev = Device(id="dw-pi", name="DW", status=DeviceStatus.ADOPTED)
        db_session.add_all([g1, g2, dev])
        await db_session.commit()
        return dev.id, g1.id, g2.id

    async def test_patch_group_id_reassigns(self, client, db_session):
        dev, g1, g2 = await self._device_and_groups(db_session)

        resp = await client.patch(f"/api/devices/{dev}", json={"group_id": str(g1)})
        assert resp.status_code == 200
        assert await _group_of(db_session, dev) == str(g1)

        resp = await client.patch(f"/api/devices/{dev}", json={"group_id": str(g2)})
        assert resp.status_code == 200
        assert await _group_of(db_session, dev) == str(g2)

        resp = await client.patch(f"/api/devices/{dev}", json={"group_id": None})
        assert resp.status_code == 200
        assert await _group_of(db_session, dev) is None

    async def test_deleting_group_ungroups_device(self, client, db_session):
        dev, g1, _ = await self._device_and_groups(db_session)
        device = await db_session.get(Device, dev)
        await set_device_group(db_session, device, g1)
        await db_session.commit()

        # ON DELETE SET NULL leaves the device intact but ungrouped.
        grp = await db_session.get(DeviceGroup, g1)
        await db_session.delete(grp)
        await db_session.commit()
        db_session.expire_all()
        assert await _group_of(db_session, dev) is None
