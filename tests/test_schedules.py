"""Tests for schedule API endpoints."""

import pytest


@pytest.mark.asyncio
class TestScheduleCRUD:
    async def _create_device_and_asset(self, db_session):
        """Helper: create a device and an asset for scheduling."""
        from cms.models.asset import Asset, AssetType
        from cms.models.device import Device, DeviceStatus

        device = Device(id="sched-pi", name="Schedule Test", status=DeviceStatus.ADOPTED)
        asset = Asset(filename="promo.mp4", asset_type=AssetType.VIDEO, size_bytes=100, checksum="aaa")
        db_session.add_all([device, asset])
        await db_session.commit()
        return device.id, str(asset.id)

    async def test_create_schedule(self, client, db_session):
        device_id, asset_id = await self._create_device_and_asset(db_session)

        resp = await client.post("/api/schedules", json={
            "name": "Morning Loop",
            "device_id": device_id,
            "asset_id": asset_id,
            "start_time": "08:00",
            "end_time": "12:00",
            "priority": 5,
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "Morning Loop"
        assert data["device_id"] == device_id
        assert data["asset_id"] == asset_id
        assert data["priority"] == 5
        assert data["enabled"] is True

    async def test_list_schedules(self, client, db_session):
        device_id, asset_id = await self._create_device_and_asset(db_session)

        await client.post("/api/schedules", json={
            "name": "S1", "device_id": device_id, "asset_id": asset_id,
            "start_time": "08:00", "end_time": "12:00",
        })
        await client.post("/api/schedules", json={
            "name": "S2", "device_id": device_id, "asset_id": asset_id,
            "start_time": "13:00", "end_time": "17:00",
        })

        resp = await client.get("/api/schedules")
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    async def test_update_schedule(self, client, db_session):
        device_id, asset_id = await self._create_device_and_asset(db_session)

        create = await client.post("/api/schedules", json={
            "name": "Old", "device_id": device_id, "asset_id": asset_id,
            "start_time": "08:00", "end_time": "12:00",
        })
        sched_id = create.json()["id"]

        resp = await client.patch(f"/api/schedules/{sched_id}", json={"name": "Updated", "priority": 10})
        assert resp.status_code == 200
        assert resp.json()["name"] == "Updated"
        assert resp.json()["priority"] == 10

    async def test_toggle_schedule(self, client, db_session):
        device_id, asset_id = await self._create_device_and_asset(db_session)

        create = await client.post("/api/schedules", json={
            "name": "Toggle Me", "device_id": device_id, "asset_id": asset_id,
            "start_time": "08:00", "end_time": "12:00",
        })
        sched_id = create.json()["id"]

        resp = await client.patch(f"/api/schedules/{sched_id}", json={"enabled": False})
        assert resp.status_code == 200
        assert resp.json()["enabled"] is False

    async def test_delete_schedule(self, client, db_session):
        device_id, asset_id = await self._create_device_and_asset(db_session)

        create = await client.post("/api/schedules", json={
            "name": "Delete Me", "device_id": device_id, "asset_id": asset_id,
            "start_time": "08:00", "end_time": "12:00",
        })
        sched_id = create.json()["id"]

        resp = await client.delete(f"/api/schedules/{sched_id}")
        assert resp.status_code == 200

        resp = await client.get("/api/schedules")
        assert len(resp.json()) == 0

    async def test_schedule_requires_target(self, client, db_session):
        _, asset_id = await self._create_device_and_asset(db_session)

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

    async def test_schedule_both_targets_rejected(self, client, db_session):
        device_id, asset_id = await self._create_device_and_asset(db_session)

        group_resp = await client.post("/api/devices/groups/", json={"name": "Both"})
        group_id = group_resp.json()["id"]

        resp = await client.post("/api/schedules", json={
            "name": "Both Targets",
            "device_id": device_id,
            "group_id": group_id,
            "asset_id": asset_id,
            "start_time": "08:00",
            "end_time": "12:00",
        })
        assert resp.status_code == 422

    async def test_schedule_with_days_of_week(self, client, db_session):
        device_id, asset_id = await self._create_device_and_asset(db_session)

        resp = await client.post("/api/schedules", json={
            "name": "Weekdays Only",
            "device_id": device_id,
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
        device_id, asset_id = await self._create_device_and_asset(db_session)
        resp = await client.post("/api/schedules", json={
            "name": "Bad Dates",
            "device_id": device_id,
            "asset_id": asset_id,
            "start_time": "08:00",
            "end_time": "12:00",
            "start_date": "2026-04-10T00:00:00Z",
            "end_date": "2026-04-05T00:00:00Z",
        })
        assert resp.status_code == 422

    async def test_reject_same_start_end_time(self, client, db_session):
        """Server should reject a schedule where start_time == end_time."""
        device_id, asset_id = await self._create_device_and_asset(db_session)
        resp = await client.post("/api/schedules", json={
            "name": "Zero Window",
            "device_id": device_id,
            "asset_id": asset_id,
            "start_time": "10:00",
            "end_time": "10:00",
        })
        assert resp.status_code == 422

    async def test_update_reject_end_date_before_start_date(self, client, db_session):
        """Server should reject an update that sets end_date < start_date."""
        device_id, asset_id = await self._create_device_and_asset(db_session)
        create = await client.post("/api/schedules", json={
            "name": "Will Edit",
            "device_id": device_id,
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
        """Server should reject a schedule that conflicts (same device, same priority, overlapping time)."""
        device_id, asset_id = await self._create_device_and_asset(db_session)
        resp1 = await client.post("/api/schedules", json={
            "name": "Morning",
            "device_id": device_id,
            "asset_id": asset_id,
            "start_time": "08:00",
            "end_time": "12:00",
        })
        assert resp1.status_code == 201
        resp2 = await client.post("/api/schedules", json={
            "name": "Overlap",
            "device_id": device_id,
            "asset_id": asset_id,
            "start_time": "10:00",
            "end_time": "14:00",
        })
        assert resp2.status_code == 409
        assert "Conflicts with" in resp2.json()["detail"]

    async def test_allow_overlap_different_priority(self, client, db_session):
        """Overlapping schedules with different priorities are allowed."""
        device_id, asset_id = await self._create_device_and_asset(db_session)
        await client.post("/api/schedules", json={
            "name": "Low",
            "device_id": device_id,
            "asset_id": asset_id,
            "start_time": "08:00",
            "end_time": "12:00",
            "priority": 0,
        })
        resp = await client.post("/api/schedules", json={
            "name": "High",
            "device_id": device_id,
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
        from cms.models.device import Device, DeviceStatus

        device = Device(id="edit-pi", name="Edit Test", status=DeviceStatus.ADOPTED)
        asset = Asset(filename="edit.mp4", asset_type=AssetType.VIDEO, size_bytes=100, checksum="eee")
        db_session.add_all([device, asset])
        await db_session.commit()

        # Create a schedule so the Edit button is rendered
        resp = await client.post("/api/schedules", json={
            "name": "Editable",
            "device_id": "edit-pi",
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

    async def test_timezone_labels_no_underscores(self, client):
        """Timezone dropdown labels should use spaces, not underscores."""
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


@pytest.mark.asyncio
class TestScheduleNaiveDatetime:
    """Regression: naive datetime strings from the browser caused a 500 error
    when updating schedules because the scheduler compared them with
    timezone-aware UTC datetimes.

    TypeError: can't compare offset-naive and offset-aware datetimes
    """

    async def _create_device_and_asset(self, db_session):
        from cms.models.asset import Asset, AssetType
        from cms.models.device import Device, DeviceStatus

        device = Device(id="tz-pi", name="TZ Test", status=DeviceStatus.ADOPTED)
        asset = Asset(filename="tz.mp4", asset_type=AssetType.VIDEO, size_bytes=100, checksum="ttt")
        db_session.add_all([device, asset])
        await db_session.commit()
        return device.id, str(asset.id)

    async def test_create_with_naive_date_strings(self, client, db_session):
        """Creating a schedule with naive date strings (no timezone) should succeed."""
        device_id, asset_id = await self._create_device_and_asset(db_session)
        resp = await client.post("/api/schedules", json={
            "name": "Naive Dates",
            "device_id": device_id,
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
        device_id, asset_id = await self._create_device_and_asset(db_session)

        # Create without dates
        create = await client.post("/api/schedules", json={
            "name": "Edit Dates",
            "device_id": device_id,
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
        device_id, asset_id = await self._create_device_and_asset(db_session)

        # Create
        create = await client.post("/api/schedules", json={
            "name": "Full Flow",
            "device_id": device_id,
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
            "device_id": device_id,
            "group_id": None,
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
        from cms.models.device import Device, DeviceStatus

        device = Device(id="end-now-pi", name="End Now Test", status=DeviceStatus.ADOPTED)
        asset = Asset(filename="promo.mp4", asset_type=AssetType.VIDEO, size_bytes=100, checksum="bbb")
        db_session.add_all([device, asset])
        await db_session.commit()
        return device.id, str(asset.id)

    async def test_patch_clears_end_now_skip(self, client, db_session):
        """PATCH /api/schedules/<id> should clear any active End Now skip."""
        from cms.services.scheduler import _skipped

        device_id, asset_id = await self._seed(db_session)

        # Create a schedule
        create = await client.post("/api/schedules", json={
            "name": "End Now Test",
            "device_id": device_id,
            "asset_id": asset_id,
            "start_time": "08:00",
            "end_time": "17:00",
        })
        assert create.status_code == 201
        sched_id = create.json()["id"]

        # End it now
        resp = await client.post(f"/api/schedules/{sched_id}/end-now")
        assert resp.status_code == 200
        assert sched_id in _skipped

        # Edit the schedule (even without real changes)
        resp = await client.patch(f"/api/schedules/{sched_id}", json={"name": "End Now Test"})
        assert resp.status_code == 200

        # The skip should be cleared
        assert sched_id not in _skipped

    async def test_toggle_enabled_clears_skip(self, client, db_session):
        """Toggling enabled on a skipped schedule should clear the skip."""
        from cms.services.scheduler import _skipped

        device_id, asset_id = await self._seed(db_session)

        create = await client.post("/api/schedules", json={
            "name": "Toggle Skip Test",
            "device_id": device_id,
            "asset_id": asset_id,
            "start_time": "08:00",
            "end_time": "17:00",
        })
        sched_id = create.json()["id"]

        # End it now
        await client.post(f"/api/schedules/{sched_id}/end-now")
        assert sched_id in _skipped

        # Toggle enabled off then on
        await client.patch(f"/api/schedules/{sched_id}", json={"enabled": False})
        assert sched_id not in _skipped
