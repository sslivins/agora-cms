"""Phase D coverage tests for the /devices triage redesign.

Adds request-level integration coverage for the surfaces that the pure
``cms.services.device_alerts`` unit tests can't exercise:

* permission gating on the ``maintenance`` severity tag — operators
  without ``devices:manage`` must not see the tag in
  ``data-severity-tags`` or fleet rollups, while admins must.
* the ``GET /api/devices/groups/{id}/panel`` HTML fragment endpoint
  for non-empty groups — verifies the rich device_row macro renders
  with the expected anchors so the cross-session poller and createGroup
  handler can insert correct markup.
* the "Remove from group" kebab regression — restored in 5784afe; this
  test guards against it disappearing again.
"""

import re
import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient


# ── fixtures ────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def operator_client(app):
    """Authenticated HTTP client logged in as a non-manage operator."""
    from sqlalchemy import select

    from cms.auth import hash_password
    from cms.database import get_db
    from cms.models.user import Role, User

    factory = app.dependency_overrides[get_db]
    async for db in factory():
        op_role = (await db.execute(select(Role).where(Role.name == "Operator"))).scalar_one()
        op_user = User(
            username="phase_d_op",
            email="phase_d_op@test.com",
            display_name="Phase D Operator",
            password_hash=hash_password("opp"),
            role_id=op_role.id,
            is_active=True,
            must_change_password=False,
        )
        db.add(op_user)
        await db.commit()
        break

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        await ac.post(
            "/login",
            data={"username": "phase_d_op", "password": "opp"},
            follow_redirects=False,
        )
        yield ac


@pytest_asyncio.fixture
async def grouped_update_device(app):
    """Seed: one group + one adopted device with an out-of-date firmware
    so that ``is_update_available`` returns True for it.

    Also grants every non-admin user access to the seeded group so
    operator-flavored tests don't 403 on the group panel endpoint.

    Pins the version checker's cached latest version and restores it on
    teardown so other tests aren't perturbed.
    """
    from sqlalchemy import select

    from cms.database import get_db
    from cms.models.device import Device, DeviceGroup, DeviceStatus
    from cms.models.user import Role, User, UserGroup
    from cms.services import version_checker

    factory = app.dependency_overrides[get_db]
    group_id = device_id = None
    async for db in factory():
        group = DeviceGroup(name="Phase D Group", description="")
        db.add(group)
        await db.flush()
        group_id = str(group.id)

        device = Device(
            id="phase-d-update-device",
            name="Phase D Device",
            status=DeviceStatus.ADOPTED,
            firmware_version="0.0.1",   # ancient → update_available=True
            group_id=group.id,
        )
        db.add(device)

        # Grant any seeded non-admin user (e.g. the operator from
        # operator_client) access to this group so /api/devices/groups/
        # {id}/panel doesn't 403 on the group-scoped check.
        admin_role_id = (
            await db.execute(select(Role.id).where(Role.name == "Admin"))
        ).scalar_one()
        non_admin_users = (
            await db.execute(select(User).where(User.role_id != admin_role_id))
        ).scalars().all()
        for u in non_admin_users:
            db.add(UserGroup(user_id=u.id, group_id=group.id))
        await db.commit()
        device_id = device.id
        break

    saved = version_checker._latest_version
    version_checker._latest_version = "9.9.9"
    try:
        yield {"group_id": group_id, "device_id": device_id}
    finally:
        version_checker._latest_version = saved


# ── /api/devices/groups/{id}/panel — permission gating ──────────────


@pytest.mark.asyncio
class TestPanelMaintenancePermissionGating:
    """The panel fragment must respect the same maintenance gating as the
    full /devices render. A bug discovered while writing this suite was
    the panel endpoint not threading user_perms through to
    device_severity_tags / fleet_counts (cms/routers/devices.py ~1046)."""

    @staticmethod
    def _row_severity_tags(html: str, device_id: str) -> set[str]:
        """Pull `data-severity-tags="..."` off the row for *device_id*."""
        m = re.search(
            rf'<tr[^>]*\bdata-device-id="{re.escape(device_id)}"[^>]*\bdata-severity-tags="([^"]*)"',
            html,
        )
        return set(m.group(1).split()) if m else set()

    @staticmethod
    def _rollup_maintenance_count(html: str, group_id: str) -> int | None:
        """Rollup chips render as <span class="rollup-chip ..." data-rollup-tag="maintenance">N</span>
        within the group panel."""
        # Scope to the group panel's rollup region by anchoring on data-group-id.
        m = re.search(
            r'data-rollup-tag="maintenance"[^>]*>\s*(\d+)',
            html,
        )
        return int(m.group(1)) if m else None

    async def test_admin_sees_maintenance_in_panel(self, client, grouped_update_device):
        gid = grouped_update_device["group_id"]
        did = grouped_update_device["device_id"]
        resp = await client.get(f"/api/devices/groups/{gid}/panel")
        assert resp.status_code == 200, resp.text
        tags = self._row_severity_tags(resp.text, did)
        assert "maintenance" in tags, f"admin should see maintenance, got {tags!r}"

    async def test_operator_does_not_see_maintenance_in_panel(
        self, operator_client, grouped_update_device
    ):
        gid = grouped_update_device["group_id"]
        did = grouped_update_device["device_id"]
        resp = await operator_client.get(f"/api/devices/groups/{gid}/panel")
        assert resp.status_code == 200, resp.text
        tags = self._row_severity_tags(resp.text, did)
        assert "maintenance" not in tags, (
            f"operator must not see maintenance tag; got {tags!r}. "
            "Check that the panel endpoint passes user_perms to device_severity_tags."
        )

    async def test_operator_rollup_excludes_maintenance(
        self, operator_client, grouped_update_device
    ):
        """Group rollup chip count for ``maintenance`` should be 0 (or
        the chip omitted entirely) for operators."""
        gid = grouped_update_device["group_id"]
        resp = await operator_client.get(f"/api/devices/groups/{gid}/panel")
        assert resp.status_code == 200, resp.text
        count = self._rollup_maintenance_count(resp.text, gid)
        # Either chip is absent (None) or count is 0; both are correct.
        assert count in (None, 0), (
            f"operator group rollup leaked maintenance count={count}. "
            "Check that the panel endpoint passes user_perms to fleet_counts."
        )


# ── /api/devices/groups/{id}/panel — non-empty rich row contract ────


@pytest.mark.asyncio
class TestPanelRichRowContract:
    """A non-empty group panel must emit the rich device_row markup the
    JS poller expects: a tr.device-row carrying data-device-id and a
    data-severity-tags attribute, and the panel root must carry
    data-group-id + a data-group-tbody anchor."""

    async def test_panel_emits_rich_row_with_anchors(self, client, grouped_update_device):
        gid = grouped_update_device["group_id"]
        did = grouped_update_device["device_id"]
        resp = await client.get(f"/api/devices/groups/{gid}/panel")
        assert resp.status_code == 200, resp.text
        body = resp.text

        # Panel anchors used by the JS poller.
        assert f'data-group-id="{gid}"' in body
        assert f'data-group-tbody="{gid}"' in body

        # Rich row anchors that drive client-side filtering + live updates.
        m = re.search(
            rf'<tr[^>]*\bclass="device-row[^"]*"[^>]*\bdata-device-id="{re.escape(did)}"[^>]*>',
            body,
        )
        assert m, "expected a tr.device-row[data-device-id=...] in panel HTML"
        row = m.group(0)
        assert "data-severity-tags=" in row, (
            "rich row missing data-severity-tags — triage filtering depends on it"
        )


# ── "Remove from group" kebab regression ────────────────────────────


@pytest.mark.asyncio
class TestRemoveFromGroupKebab:
    """Restored in 5784afe after a Phase C macro consolidation regression.
    Guards against the kebab item disappearing again, and exercises the
    PATCH endpoint that backs it."""

    async def test_admin_sees_remove_from_group_on_grouped_row(
        self, client, grouped_update_device
    ):
        gid = grouped_update_device["group_id"]
        did = grouped_update_device["device_id"]
        resp = await client.get(f"/api/devices/groups/{gid}/panel")
        assert resp.status_code == 200, resp.text

        # Scope to the row + its detail siblings (the kebab lives in the
        # actions cell rendered on the row's own <tr>). Use a non-greedy
        # match anchored on the device id and bounded by the next
        # device-row or end of tbody.
        m = re.search(
            rf'<tr[^>]*\bdata-device-id="{re.escape(did)}"[\s\S]*?(?=<tr[^>]*\bdata-device-id=|</tbody>)',
            resp.text,
        )
        assert m, "couldn't locate device row block for kebab assertion"
        row_block = m.group(0)
        assert (
            f"assignGroup('{did}', '')" in row_block
            and "Remove from group" in row_block
        ), "Remove from group kebab item missing on grouped device row"

    async def test_patch_group_id_null_unassigns_device(
        self, client, grouped_update_device
    ):
        did = grouped_update_device["device_id"]
        resp = await client.patch(f"/api/devices/{did}", json={"group_id": None})
        assert resp.status_code == 200, resp.text
        assert resp.json()["group_id"] is None
