"""Integration coverage for the Stage 7 devices-page many-to-many UI."""

import re

import pytest

from cms.models.device import Device, DeviceGroup, DeviceStatus
from cms.models.device_group_membership import DeviceGroupMembership


@pytest.mark.asyncio
class TestDevicesPageManyToMany:
    async def test_device_renders_in_each_group_panel(self, client, db_session):
        group_a = DeviceGroup(name="UI Alpha", description="")
        group_b = DeviceGroup(name="UI Beta", description="")
        db_session.add_all([group_a, group_b])
        await db_session.flush()

        device = Device(
            id="ui-g2m-001",
            name="UI G2M Device",
            status=DeviceStatus.ADOPTED,
            group_id=group_a.id,
        )
        db_session.add(device)
        await db_session.flush()
        db_session.add_all(
            [
                DeviceGroupMembership(device_id=device.id, group_id=group_a.id),
                DeviceGroupMembership(device_id=device.id, group_id=group_b.id),
            ]
        )
        await db_session.commit()

        resp = await client.get("/devices")
        assert resp.status_code == 200, resp.text
        body = resp.text

        assert body.count('data-device-id="ui-g2m-001"') >= 2
        assert f'data-group-ids="{group_a.id} {group_b.id}"' in body

        alpha_block = re.search(
            rf'data-group-id="{re.escape(str(group_a.id))}"[\s\S]*?data-device-id="ui-g2m-001"',
            body,
        )
        beta_block = re.search(
            rf'data-group-id="{re.escape(str(group_b.id))}"[\s\S]*?data-device-id="ui-g2m-001"',
            body,
        )
        assert alpha_block, "device should render in the first group panel"
        assert beta_block, "device should render in the second group panel"

        ungrouped_match = re.search(
            r'<div class="card" id="ungrouped-section"[\s\S]*?data-device-id="ui-g2m-001"',
            body,
        )
        assert not ungrouped_match, "multi-group device should not appear ungrouped"
