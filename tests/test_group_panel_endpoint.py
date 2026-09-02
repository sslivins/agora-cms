"""Unit test for GET /api/devices/groups/{id}/panel — the HTML fragment
endpoint used by the /devices page's cross-session poller and createGroup
handler to insert a group panel without a full page reload (issue #87)."""

import pytest

from cms.models.device import Device, DeviceGroup, DeviceStatus
from cms.models.device_group_membership import DeviceGroupMembership


@pytest.mark.asyncio
class TestGroupPanelEndpoint:
    async def test_panel_returns_html_fragment(self, client):
        resp = await client.post(
            "/api/devices/groups/",
            json={"name": "Fragment Test Group", "description": "fragment desc"},
        )
        assert resp.status_code == 201, resp.text
        group_id = resp.json()["id"]

        panel = await client.get(f"/api/devices/groups/{group_id}/panel")
        assert panel.status_code == 200
        body = panel.text
        # The root element carries the group id so the client can locate it.
        assert f'data-group-id="{group_id}"' in body
        # Header renders the name + description.
        assert "Fragment Test Group" in body
        assert "fragment desc" in body
        # Empty group renders the "No devices in this group" empty-state.
        assert "No devices in this group" in body
        # Body table gets a data-group-tbody anchor the poller looks for.
        assert f'data-group-tbody="{group_id}"' in body

    async def test_panel_unknown_id_404(self, client):
        resp = await client.get(
            "/api/devices/groups/00000000-0000-0000-0000-000000000000/panel"
        )
        assert resp.status_code == 404

    async def test_panel_uses_effective_many_to_many_membership(
        self, client, db_session
    ):
        group_a = DeviceGroup(name="Panel Alpha", description="")
        group_b = DeviceGroup(name="Panel Beta", description="")
        db_session.add_all([group_a, group_b])
        await db_session.flush()

        device = Device(
            id="panel-m2m-001",
            name="Panel M2M Device",
            status=DeviceStatus.ADOPTED,
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

        panel = await client.get(f"/api/devices/groups/{group_b.id}/panel")
        assert panel.status_code == 200, panel.text
        assert 'data-device-id="panel-m2m-001"' in panel.text
        assert f'data-group-ids="{group_a.id} {group_b.id}"' in panel.text
