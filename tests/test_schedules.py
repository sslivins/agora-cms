"""Tests for schedule API endpoints."""

import pytest


@pytest.mark.asyncio
class TestScheduleCRUD:
    async def _create_group_and_asset(self, db_session):
        """Helper: create a device, group, and asset for scheduling."""
        from cms.models.asset import Asset, AssetType
        from cms.models.device import Device, DeviceGroup, DeviceStatus

        group = DeviceGroup(name="Test Group")
        device = Device(id="sched-pi", name="Schedule Test", status=DeviceStatus.ADOPTED)
        asset = Asset(filename="promo.mp4", asset_type=AssetType.VIDEO, size_bytes=100, checksum="aaa")
        db_session.add_all([group, device, asset])
        await db_session.flush()
        device.group_id = group.id
        await db_session.commit()
        return str(group.id), str(asset.id)

    async def test_create_schedule(self, client, db_session):
        group_id, asset_id = await self._create_group_and_asset(db_session)

        resp = await client.post("/api/schedules", json={
            "name": "Morning Loop",
            "group_id": group_id,
            "asset_id": asset_id,
            "start_time": "08:00",
            "end_time": "12:00",
            "priority": 5,
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "Morning Loop"
        assert data["asset_id"] == asset_id
        assert data["priority"] == 5
        assert data["enabled"] is True

    async def test_list_schedules(self, client, db_session):
        group_id, asset_id = await self._create_group_and_asset(db_session)

        await client.post("/api/schedules", json={
            "name": "S1", "group_id": group_id, "asset_id": asset_id,
            "start_time": "08:00", "end_time": "12:00",
        })
        await client.post("/api/schedules", json={
            "name": "S2", "group_id": group_id, "asset_id": asset_id,
            "start_time": "13:00", "end_time": "17:00",
        })

        resp = await client.get("/api/schedules")
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    async def test_update_schedule(self, client, db_session):
        group_id, asset_id = await self._create_group_and_asset(db_session)

        create = await client.post("/api/schedules", json={
            "name": "Old", "group_id": group_id, "asset_id": asset_id,
            "start_time": "08:00", "end_time": "12:00",
        })
        sched_id = create.json()["id"]

        resp = await client.patch(f"/api/schedules/{sched_id}", json={"name": "Updated", "priority": 10})
        assert resp.status_code == 200
        assert resp.json()["name"] == "Updated"
        assert resp.json()["priority"] == 10

    async def test_toggle_schedule(self, client, db_session):
        group_id, asset_id = await self._create_group_and_asset(db_session)

        create = await client.post("/api/schedules", json={
            "name": "Toggle Me", "group_id": group_id, "asset_id": asset_id,
            "start_time": "08:00", "end_time": "12:00",
        })
        sched_id = create.json()["id"]

        resp = await client.patch(f"/api/schedules/{sched_id}", json={"enabled": False})
        assert resp.status_code == 200
        assert resp.json()["enabled"] is False

    async def test_delete_schedule(self, client, db_session):
        group_id, asset_id = await self._create_group_and_asset(db_session)

        create = await client.post("/api/schedules", json={
            "name": "Delete Me", "group_id": group_id, "asset_id": asset_id,
            "start_time": "08:00", "end_time": "12:00",
        })
        sched_id = create.json()["id"]

        resp = await client.delete(f"/api/schedules/{sched_id}")
        assert resp.status_code == 200

        resp = await client.get("/api/schedules")
        assert len(resp.json()) == 0

    async def test_schedule_requires_target(self, client, db_session):
        _, asset_id = await self._create_group_and_asset(db_session)

        resp = await client.post("/api/schedules", json={
            "name": "No Target", "asset_id": asset_id,
            "start_time": "08:00", "end_time": "12:00",
        })
        assert resp.status_code == 422  # Validation error

    async def test_schedule_with_group(self, client, db_session):
        from cms.models.asset import Asset, AssetType

        asset = Asset(filename="group-vid.mp4", asset_type=AssetType.VIDEO, size_bytes=100, checksum="bbb")
        db_session.add(asset)
        await db_session.commit()

        group_resp = await client.post("/api/devices/groups/", json={"name": "Lobby"})
        group_id = group_resp.json()["id"]

        resp = await client.post("/api/schedules", json={
            "name": "Group Schedule",
            "group_id": group_id,
            "asset_id": str(asset.id),
            "start_time": "09:00",
            "end_time": "18:00",
        })
        assert resp.status_code == 201
        assert resp.json()["group_id"] == group_id

    async def test_schedule_with_days_of_week(self, client, db_session):
        group_id, asset_id = await self._create_group_and_asset(db_session)

        resp = await client.post("/api/schedules", json={
            "name": "Weekdays Only",
            "group_id": group_id,
            "asset_id": asset_id,
            "start_time": "08:00",
            "end_time": "17:00",
            "days_of_week": [1, 2, 3, 4, 5],
        })
        assert resp.status_code == 201
        assert resp.json()["days_of_week"] == [1, 2, 3, 4, 5]

    async def test_get_nonexistent(self, client):
        resp = await client.get("/api/schedules/00000000-0000-0000-0000-000000000000")
        assert resp.status_code == 404

    async def test_requires_auth(self, unauthed_client):
        resp = await unauthed_client.get("/api/schedules")
        assert resp.status_code in (401, 303)

    async def test_reject_end_date_before_start_date(self, client, db_session):
        """Server should reject a schedule where end_date < start_date."""
        group_id, asset_id = await self._create_group_and_asset(db_session)
        resp = await client.post("/api/schedules", json={
            "name": "Bad Dates",
            "group_id": group_id,
            "asset_id": asset_id,
            "start_time": "08:00",
            "end_time": "12:00",
            "start_date": "2026-04-10T00:00:00Z",
            "end_date": "2026-04-05T00:00:00Z",
        })
        assert resp.status_code == 422

    async def test_reject_same_start_end_time(self, client, db_session):
        """Server should reject a schedule where start_time == end_time."""
        group_id, asset_id = await self._create_group_and_asset(db_session)
        resp = await client.post("/api/schedules", json={
            "name": "Zero Window",
            "group_id": group_id,
            "asset_id": asset_id,
            "start_time": "10:00",
            "end_time": "10:00",
        })
        assert resp.status_code == 422

    async def test_update_reject_end_date_before_start_date(self, client, db_session):
        """Server should reject an update that sets end_date < start_date."""
        group_id, asset_id = await self._create_group_and_asset(db_session)
        create = await client.post("/api/schedules", json={
            "name": "Will Edit",
            "group_id": group_id,
            "asset_id": asset_id,
            "start_time": "08:00",
            "end_time": "12:00",
        })
        sid = create.json()["id"]
        resp = await client.patch(f"/api/schedules/{sid}", json={
            "start_date": "2026-04-10T00:00:00Z",
            "end_date": "2026-04-05T00:00:00Z",
        })
        assert resp.status_code == 422

    async def test_reject_conflict_same_priority(self, client, db_session):
        """Server should reject a schedule that conflicts (same group, same priority, overlapping time)."""
        group_id, asset_id = await self._create_group_and_asset(db_session)
        resp1 = await client.post("/api/schedules", json={
            "name": "Morning",
            "group_id": group_id,
            "asset_id": asset_id,
            "start_time": "08:00",
            "end_time": "12:00",
        })
        assert resp1.status_code == 201
        resp2 = await client.post("/api/schedules", json={
            "name": "Overlap",
            "group_id": group_id,
            "asset_id": asset_id,
            "start_time": "10:00",
            "end_time": "14:00",
        })
        assert resp2.status_code == 409
        assert "Conflicts with" in resp2.json()["detail"]

    async def test_allow_overlap_different_priority(self, client, db_session):
        """Overlapping schedules with different priorities are allowed."""
        group_id, asset_id = await self._create_group_and_asset(db_session)
        await client.post("/api/schedules", json={
            "name": "Low",
            "group_id": group_id,
            "asset_id": asset_id,
            "start_time": "08:00",
            "end_time": "12:00",
            "priority": 0,
        })
        resp = await client.post("/api/schedules", json={
            "name": "High",
            "group_id": group_id,
            "asset_id": asset_id,
            "start_time": "08:00",
            "end_time": "12:00",
            "priority": 5,
        })
        assert resp.status_code == 201


@pytest.mark.asyncio
class TestScheduleUI:
    async def test_edit_schedule_modal_works(self, client, db_session):
        """Schedules page JS must parse without errors so editSchedule() is defined.

        Regression: a duplicate ``const endDate`` declaration in
        ``updateScheduleSummary()`` caused a SyntaxError at parse time,
        preventing **all** functions in the script block from loading —
        including ``editSchedule()``.  The edit button silently did nothing.
        """
        from cms.models.asset import Asset, AssetType
        from cms.models.device import Device, DeviceGroup, DeviceStatus

        group = DeviceGroup(name="Edit Group")
        device = Device(id="edit-pi", name="Edit Test", status=DeviceStatus.ADOPTED)
        asset = Asset(filename="edit.mp4", asset_type=AssetType.VIDEO, size_bytes=100, checksum="eee")
        db_session.add_all([group, device, asset])
        await db_session.flush()
        device.group_id = group.id
        await db_session.commit()

        # Create a schedule so the Edit button is rendered
        resp = await client.post("/api/schedules", json={
            "name": "Editable",
            "group_id": str(group.id),
            "asset_id": str(asset.id),
            "start_time": "09:00",
            "end_time": "17:00",
        })
        assert resp.status_code == 201

        # Render the schedules page
        page = await client.get("/schedules")
        assert page.status_code == 200
        html = page.text

        # The Edit button must be present with a valid onclick handler
        assert "editSchedule(" in html

        # No duplicate const declarations within the same function body —
        # this would be a SyntaxError that silently breaks all JS on the page.
        import re
        for script in re.findall(r"<script[^>]*>(.*?)</script>", html, re.DOTALL):
            # Extract function bodies and check each for duplicate const names
            for fn_match in re.finditer(r"function\s+(\w+)\s*\([^)]*\)\s*\{", script):
                fn_name = fn_match.group(1)
                # Find the function body (from opening { to matching })
                start = fn_match.end() - 1
                depth = 0
                end = start
                for i in range(start, len(script)):
                    if script[i] == "{":
                        depth += 1
                    elif script[i] == "}":
                        depth -= 1
                        if depth == 0:
                            end = i
                            break
                body = script[start:end]
                # Find top-level const declarations in this function body
                # (skip nested function/arrow bodies for simplicity)
                consts = re.findall(r"\bconst\s+(\w+)\b", body)
                from collections import Counter
                counts = Counter(consts)
                dupes = {name: n for name, n in counts.items() if n > 1}
                assert not dupes, (
                    f"Duplicate const in {fn_name}() will cause SyntaxError: {dupes}"
                )

    async def test_timezone_labels_no_underscores(self, client, db_session):
        """Timezone dropdown labels should use spaces, not underscores."""
        from cms.models.asset import Asset, AssetType
        from cms.models.device import DeviceGroup

        # The form (and timezone dropdown) only renders when groups+assets exist
        db_session.add(DeviceGroup(name="TZ Test"))
        db_session.add(Asset(filename="tz.mp4", asset_type=AssetType.VIDEO,
                             size_bytes=100, checksum="tz1"))
        await db_session.commit()

        resp = await client.get("/schedules")
        assert resp.status_code == 200
        html = resp.text
        # Find all timezone option labels — they look like: >America/New York (UTC-04:00)<
        import re
        labels = re.findall(r'>([^<]+\(UTC[+-]\d{2}:\d{2}\))<', html)
        assert len(labels) > 0, "Should find timezone options in the page"
        for label in labels:
            # The part before the UTC offset should not have underscores
            name_part = label.split(" (UTC")[0]
            assert "_" not in name_part, f"Timezone label has underscore: {label}"

    async def test_no_groups_shows_warning(self, client, db_session):
        """When no groups exist, the create form should show a warning instead."""
        resp = await client.get("/schedules")
        assert resp.status_code == 200
        html = resp.text
        assert "No groups available" in html
        assert "create a group" in html
        # The create form submit button should NOT be present
        assert 'Create Schedule</button>' not in html

    async def test_no_assets_shows_warning(self, client, db_session):
        """When groups exist but no assets, show an upload warning."""
        from cms.models.device import DeviceGroup
        db_session.add(DeviceGroup(name="Has Group"))
        await db_session.commit()

        resp = await client.get("/schedules")
        assert resp.status_code == 200
        html = resp.text
        assert "No assets uploaded" in html
        assert "upload an asset" in html
        assert 'Create Schedule</button>' not in html

    async def test_groups_and_assets_shows_form(self, client, db_session):
        """When both groups and assets exist, the create form should render."""
        from cms.models.asset import Asset, AssetType
        from cms.models.device import DeviceGroup

        db_session.add(DeviceGroup(name="Lobby"))
        db_session.add(Asset(filename="clip.mp4", asset_type=AssetType.VIDEO,
                             size_bytes=100, checksum="xyz"))
        await db_session.commit()

        resp = await client.get("/schedules")
        assert resp.status_code == 200
        html = resp.text
        assert "No groups available" not in html
        assert "No assets uploaded" not in html
        assert 'Create Schedule</button>' in html


@pytest.mark.asyncio
class TestScheduleDeletePlayingWarning:
    """Verify the schedules page injects _playingScheduleIds so the JS
    delete confirmation can warn when a schedule is currently playing."""

    async def _seed(self, db_session):
        from cms.models.asset import Asset, AssetType
        from cms.models.device import Device, DeviceGroup, DeviceStatus
        from cms.models.schedule import Schedule
        from datetime import time

        group = DeviceGroup(name="Lobby")
        device = Device(id="warn-pi", name="Warning Test", status=DeviceStatus.ADOPTED)
        asset = Asset(filename="ad.mp4", asset_type=AssetType.VIDEO,
                      size_bytes=100, checksum="warn1")
        db_session.add_all([group, device, asset])
        await db_session.flush()
        device.group_id = group.id

        sched = Schedule(name="Active Ad", asset_id=asset.id, group_id=group.id,
                         start_time=time(0, 0), end_time=time(23, 59), priority=1)
        db_session.add(sched)
        await db_session.commit()
        return str(sched.id), str(device.id)

    async def test_playing_schedule_id_injected(self, client, db_session):
        """When a schedule is currently playing, its ID appears in _playingScheduleIds."""
        from unittest.mock import patch, AsyncMock
        sched_id, device_id = await self._seed(db_session)

        fake_np = [{"schedule_id": sched_id, "device_id": device_id, "source": "confirmed"}]
        with patch(
            "cms.services.scheduler.compute_now_playing",
            new=AsyncMock(return_value=fake_np),
        ):
            resp = await client.get("/schedules")

        assert resp.status_code == 200
        assert sched_id in resp.text
        assert "_playingScheduleIds" in resp.text

    async def test_no_playing_schedule_empty_array(self, client, db_session):
        """When nothing is playing, _playingScheduleIds is an empty array."""
        await self._seed(db_session)

        from unittest.mock import patch, AsyncMock
        with patch(
            "cms.services.scheduler.compute_now_playing",
            new=AsyncMock(return_value=[]),
        ):
            resp = await client.get("/schedules")

        assert resp.status_code == 200
        assert "_playingScheduleIds = []" in resp.text


@pytest.mark.asyncio
class TestScheduleNaiveDatetime:
    """Regression: naive datetime strings from the browser caused a 500 error
    when updating schedules because the scheduler compared them with
    timezone-aware UTC datetimes.

    TypeError: can't compare offset-naive and offset-aware datetimes
    """

    async def _create_group_and_asset(self, db_session):
        from cms.models.asset import Asset, AssetType
        from cms.models.device import Device, DeviceGroup, DeviceStatus

        group = DeviceGroup(name="TZ Test Group")
        device = Device(id="tz-pi", name="TZ Test", status=DeviceStatus.ADOPTED)
        asset = Asset(filename="tz.mp4", asset_type=AssetType.VIDEO, size_bytes=100, checksum="ttt")
        db_session.add_all([group, device, asset])
        await db_session.flush()
        device.group_id = group.id
        await db_session.commit()
        return str(group.id), str(asset.id)

    async def test_create_with_naive_date_strings(self, client, db_session):
        """Creating a schedule with naive date strings (no timezone) should succeed."""
        group_id, asset_id = await self._create_group_and_asset(db_session)
        resp = await client.post("/api/schedules", json={
            "name": "Naive Dates",
            "group_id": group_id,
            "asset_id": asset_id,
            "start_time": "08:00",
            "end_time": "12:00",
            "start_date": "2026-04-01",
            "end_date": "2026-04-30",
        })
        assert resp.status_code == 201

    async def test_edit_with_naive_date_strings(self, client, db_session):
        """Editing a schedule to add naive date strings should not cause 500.

        This is the exact scenario from the browser — the edit modal sends
        dates like "2026-04-01" without timezone info.
        """
        group_id, asset_id = await self._create_group_and_asset(db_session)

        # Create without dates
        create = await client.post("/api/schedules", json={
            "name": "Edit Dates",
            "group_id": group_id,
            "asset_id": asset_id,
            "start_time": "09:00",
            "end_time": "17:00",
        })
        assert create.status_code == 201
        sched_id = create.json()["id"]

        # Edit to add dates (naive strings, as the browser sends)
        resp = await client.patch(f"/api/schedules/{sched_id}", json={
            "name": "Edited With Dates",
            "start_date": "2026-04-01",
            "end_date": "2026-04-30",
            "start_time": "10:00:00",
            "end_time": "18:00:00",
        })
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        data = resp.json()
        assert data["name"] == "Edited With Dates"
        assert "2026-04-01" in data["start_date"]
        assert "2026-04-30" in data["end_date"]

    async def test_create_then_full_edit(self, client, db_session):
        """Full create → edit flow mimicking the browser's behavior."""
        group_id, asset_id = await self._create_group_and_asset(db_session)

        # Create
        create = await client.post("/api/schedules", json={
            "name": "Full Flow",
            "group_id": group_id,
            "asset_id": asset_id,
            "start_time": "08:00",
            "end_time": "12:00",
            "priority": 5,
        })
        assert create.status_code == 201
        sched_id = create.json()["id"]

        # Edit everything (exactly what the edit modal sends)
        resp = await client.patch(f"/api/schedules/{sched_id}", json={
            "name": "Full Flow Updated",
            "asset_id": asset_id,
            "group_id": group_id,
            "start_time": "10:00:00",
            "end_time": "18:00:00",
            "start_date": "2026-05-01",
            "end_date": "2026-05-31",
            "days_of_week": [1, 2, 3, 4, 5],
            "priority": 10,
        })
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        data = resp.json()
        assert data["name"] == "Full Flow Updated"
        assert data["priority"] == 10
        assert data["days_of_week"] == [1, 2, 3, 4, 5]
        assert "2026-05-01" in data["start_date"]


@pytest.mark.asyncio
class TestEndNowClearedOnEdit:
    """Editing a schedule that was ended early should clear the skip."""

    async def _seed(self, db_session):
        from cms.models.asset import Asset, AssetType
        from cms.models.device import Device, DeviceGroup, DeviceStatus

        group = DeviceGroup(name="End Now Group")
        device = Device(id="end-now-pi", name="End Now Test", status=DeviceStatus.ADOPTED)
        asset = Asset(filename="promo.mp4", asset_type=AssetType.VIDEO, size_bytes=100, checksum="bbb")
        db_session.add_all([group, device, asset])
        await db_session.flush()
        device.group_id = group.id
        await db_session.commit()
        return str(group.id), str(asset.id)

    async def test_patch_clears_end_now_skip(self, client, db_session):
        """PATCH /api/schedules/<id> should clear any active End Now skip."""
        from sqlalchemy import select
        from cms.models.schedule import Schedule
        import uuid

        group_id, asset_id = await self._seed(db_session)

        # Create a schedule
        create = await client.post("/api/schedules", json={
            "name": "End Now Test",
            "group_id": group_id,
            "asset_id": asset_id,
            "start_time": "08:00",
            "end_time": "17:00",
        })
        assert create.status_code == 201
        sched_id = create.json()["id"]

        # End it now
        resp = await client.post(f"/api/schedules/{sched_id}/end-now")
        assert resp.status_code == 200
        row = (await db_session.execute(
            select(Schedule.skipped_until).where(Schedule.id == uuid.UUID(sched_id))
        )).scalar_one()
        assert row is not None

        # Edit the schedule (even without real changes)
        resp = await client.patch(f"/api/schedules/{sched_id}", json={"name": "End Now Test"})
        assert resp.status_code == 200

        # The skip should be cleared in the DB
        db_session.expire_all()
        row = (await db_session.execute(
            select(Schedule.skipped_until).where(Schedule.id == uuid.UUID(sched_id))
        )).scalar_one()
        assert row is None

    async def test_toggle_enabled_clears_skip(self, client, db_session):
        """Toggling enabled on a skipped schedule should clear the skip."""
        from sqlalchemy import select
        from cms.models.schedule import Schedule
        import uuid

        group_id, asset_id = await self._seed(db_session)

        create = await client.post("/api/schedules", json={
            "name": "Toggle Skip Test",
            "group_id": group_id,
            "asset_id": asset_id,
            "start_time": "08:00",
            "end_time": "17:00",
        })
        sched_id = create.json()["id"]

        # End it now
        await client.post(f"/api/schedules/{sched_id}/end-now")
        db_session.expire_all()
        row = (await db_session.execute(
            select(Schedule.skipped_until).where(Schedule.id == uuid.UUID(sched_id))
        )).scalar_one()
        assert row is not None

        # Toggle enabled off then on
        await client.patch(f"/api/schedules/{sched_id}", json={"enabled": False})
        db_session.expire_all()
        row = (await db_session.execute(
            select(Schedule.skipped_until).where(Schedule.id == uuid.UUID(sched_id))
        )).scalar_one()
        assert row is None


@pytest.mark.asyncio
class TestEndNowPerDevice:
    """Issue #240: End Now on one device must not stop the schedule on others."""

    async def _seed_two_devices(self, db_session):
        from cms.models.asset import Asset, AssetType
        from cms.models.device import Device, DeviceGroup, DeviceStatus

        group = DeviceGroup(name="Two Device Group")
        d1 = Device(id="pi-240-a", name="A", status=DeviceStatus.ADOPTED)
        d2 = Device(id="pi-240-b", name="B", status=DeviceStatus.ADOPTED)
        asset = Asset(filename="p.mp4", asset_type=AssetType.VIDEO, size_bytes=1, checksum="c")
        db_session.add_all([group, d1, d2, asset])
        await db_session.flush()
        d1.group_id = group.id
        d2.group_id = group.id
        await db_session.commit()
        return str(group.id), str(asset.id), d1.id, d2.id

    async def test_end_now_with_device_id_scopes_skip(self, client, db_session):
        from cms.models.schedule_device_skip import ScheduleDeviceSkip
        from cms.models.schedule import Schedule
        from sqlalchemy import select

        group_id, asset_id, dev_a, dev_b = await self._seed_two_devices(db_session)

        created = await client.post("/api/schedules", json={
            "name": "Per-Device End Now",
            "group_id": group_id,
            "asset_id": asset_id,
            "start_time": "00:00",
            "end_time": "23:59",
        })
        assert created.status_code == 201
        sched_id = created.json()["id"]

        resp = await client.post(
            f"/api/schedules/{sched_id}/end-now",
            json={"device_id": dev_a},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["device_id"] == dev_a

        # Schedule-wide skip must NOT be set; per-device skip must be set.
        import uuid
        db_session.expire_all()
        sched_wide = (await db_session.execute(
            select(Schedule.skipped_until).where(Schedule.id == uuid.UUID(sched_id))
        )).scalar_one()
        assert sched_wide is None

        rows = (await db_session.execute(
            select(ScheduleDeviceSkip.device_id).where(
                ScheduleDeviceSkip.schedule_id == uuid.UUID(sched_id)
            )
        )).scalars().all()
        assert sorted(rows) == [dev_a]

    async def test_end_now_without_body_still_schedule_wide(self, client, db_session):
        """Back-compat: POST with no body skips all devices on the schedule."""
        from cms.models.schedule_device_skip import ScheduleDeviceSkip
        from cms.models.schedule import Schedule
        from sqlalchemy import select
        import uuid

        group_id, asset_id, dev_a, dev_b = await self._seed_two_devices(db_session)

        created = await client.post("/api/schedules", json={
            "name": "No Body End Now",
            "group_id": group_id,
            "asset_id": asset_id,
            "start_time": "00:00",
            "end_time": "23:59",
        })
        sched_id = created.json()["id"]

        resp = await client.post(f"/api/schedules/{sched_id}/end-now")
        assert resp.status_code == 200

        db_session.expire_all()
        sched_wide = (await db_session.execute(
            select(Schedule.skipped_until).where(Schedule.id == uuid.UUID(sched_id))
        )).scalar_one()
        assert sched_wide is not None
        rows = (await db_session.execute(
            select(ScheduleDeviceSkip.device_id).where(
                ScheduleDeviceSkip.schedule_id == uuid.UUID(sched_id)
            )
        )).scalars().all()
        assert rows == []

    async def test_end_now_rejects_unknown_device(self, client, db_session):
        group_id, asset_id, dev_a, dev_b = await self._seed_two_devices(db_session)

        created = await client.post("/api/schedules", json={
            "name": "Bad Device End Now",
            "group_id": group_id,
            "asset_id": asset_id,
            "start_time": "00:00",
            "end_time": "23:59",
        })
        sched_id = created.json()["id"]

        resp = await client.post(
            f"/api/schedules/{sched_id}/end-now",
            json={"device_id": "not-a-target"},
        )
        assert resp.status_code == 400
