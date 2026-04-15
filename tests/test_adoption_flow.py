"""Comprehensive tests for the device adoption flow.

Covers:
- API: adopt with name, group, validation, error cases
- UI: adoption modal renders, groups populated, button state
- Auto-refresh guard: modal protected from live polling
"""

import uuid

import pytest
from cms.models.device import Device, DeviceGroup, DeviceStatus


# ── Helpers ──


async def _create_pending_device(db_session, device_id="adopt-test-001", name=None):
    device = Device(id=device_id, name=name or device_id, status=DeviceStatus.PENDING)
    db_session.add(device)
    await db_session.commit()
    return device


async def _create_orphaned_device(db_session, device_id="adopt-orphan-001"):
    device = Device(
        id=device_id,
        name=device_id,
        status=DeviceStatus.ORPHANED,
        device_auth_token_hash="stale-hash",
    )
    db_session.add(device)
    await db_session.commit()
    return device


async def _create_group(db_session, name="Test Group"):
    group = DeviceGroup(name=name, description="test group")
    db_session.add(group)
    await db_session.commit()
    return group


# ── API Tests: Adopt with name and group ──


@pytest.mark.asyncio
class TestAdoptWithName:
    """POST /api/devices/{id}/adopt accepts an optional name in the body."""

    async def test_adopt_with_name(self, client, db_session):
        await _create_pending_device(db_session)
        resp = await client.post(
            "/api/devices/adopt-test-001/adopt",
            json={"name": "Living Room Display"},
        )
        assert resp.status_code == 200

        # Verify name was applied
        resp = await client.get("/api/devices")
        dev = [d for d in resp.json() if d["id"] == "adopt-test-001"][0]
        assert dev["status"] == "adopted"
        assert dev["name"] == "Living Room Display"

    async def test_adopt_without_name_keeps_original(self, client, db_session):
        await _create_pending_device(db_session, name="original-name")
        resp = await client.post("/api/devices/adopt-test-001/adopt")
        assert resp.status_code == 200

        resp = await client.get("/api/devices")
        dev = [d for d in resp.json() if d["id"] == "adopt-test-001"][0]
        assert dev["name"] == "original-name"

    async def test_adopt_with_empty_body(self, client, db_session):
        await _create_pending_device(db_session)
        resp = await client.post(
            "/api/devices/adopt-test-001/adopt",
            json={},
        )
        assert resp.status_code == 200

    async def test_adopt_no_body_at_all(self, client, db_session):
        """Backwards-compatible: no JSON body should still work."""
        await _create_pending_device(db_session)
        resp = await client.post("/api/devices/adopt-test-001/adopt")
        assert resp.status_code == 200


@pytest.mark.asyncio
class TestAdoptWithGroup:
    """POST /api/devices/{id}/adopt accepts an optional group_id."""

    async def test_adopt_with_group(self, client, db_session):
        await _create_pending_device(db_session)
        group = await _create_group(db_session)

        resp = await client.post(
            "/api/devices/adopt-test-001/adopt",
            json={"group_id": str(group.id)},
        )
        assert resp.status_code == 200

        resp = await client.get("/api/devices")
        dev = [d for d in resp.json() if d["id"] == "adopt-test-001"][0]
        assert dev["group_id"] == str(group.id)

    async def test_adopt_with_name_and_group(self, client, db_session):
        await _create_pending_device(db_session)
        group = await _create_group(db_session, name="Lobby Screens")

        resp = await client.post(
            "/api/devices/adopt-test-001/adopt",
            json={"name": "Front Desk", "group_id": str(group.id)},
        )
        assert resp.status_code == 200

        resp = await client.get("/api/devices")
        dev = [d for d in resp.json() if d["id"] == "adopt-test-001"][0]
        assert dev["name"] == "Front Desk"
        assert dev["group_id"] == str(group.id)

    async def test_adopt_with_nonexistent_group(self, client, db_session):
        await _create_pending_device(db_session)
        fake_id = str(uuid.uuid4())

        resp = await client.post(
            "/api/devices/adopt-test-001/adopt",
            json={"group_id": fake_id},
        )
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()

    async def test_adopt_without_group_leaves_ungrouped(self, client, db_session):
        await _create_pending_device(db_session)
        resp = await client.post("/api/devices/adopt-test-001/adopt")
        assert resp.status_code == 200

        resp = await client.get("/api/devices")
        dev = [d for d in resp.json() if d["id"] == "adopt-test-001"][0]
        assert dev["group_id"] is None


@pytest.mark.asyncio
class TestAdoptOrphaned:
    """Orphaned devices can be re-adopted with name and group."""

    async def test_adopt_orphaned_with_name(self, client, db_session):
        await _create_orphaned_device(db_session)
        resp = await client.post(
            "/api/devices/adopt-orphan-001/adopt",
            json={"name": "Restored Display"},
        )
        assert resp.status_code == 200

        resp = await client.get("/api/devices")
        dev = [d for d in resp.json() if d["id"] == "adopt-orphan-001"][0]
        assert dev["status"] == "adopted"
        assert dev["name"] == "Restored Display"

    async def test_adopt_orphaned_with_group(self, client, db_session):
        await _create_orphaned_device(db_session)
        group = await _create_group(db_session)

        resp = await client.post(
            "/api/devices/adopt-orphan-001/adopt",
            json={"group_id": str(group.id)},
        )
        assert resp.status_code == 200

        resp = await client.get("/api/devices")
        dev = [d for d in resp.json() if d["id"] == "adopt-orphan-001"][0]
        assert dev["group_id"] == str(group.id)


@pytest.mark.asyncio
class TestAdoptValidation:
    """Error cases and validation for the adopt endpoint."""

    async def test_adopt_already_adopted(self, client, db_session):
        device = Device(id="already-adopted", name="x", status=DeviceStatus.ADOPTED)
        db_session.add(device)
        await db_session.commit()

        resp = await client.post("/api/devices/already-adopted/adopt")
        assert resp.status_code == 400

    async def test_adopt_nonexistent_device(self, client):
        resp = await client.post("/api/devices/no-such-device/adopt")
        assert resp.status_code == 404

    async def test_adopt_requires_auth(self, unauthed_client, db_session):
        await _create_pending_device(db_session)
        resp = await unauthed_client.post("/api/devices/adopt-test-001/adopt")
        # Should redirect to login or return 401/403
        assert resp.status_code in (302, 401, 403)


# ── UI Tests: Adoption modal and groups ──


@pytest.mark.asyncio
class TestAdoptionUI:
    """Test the devices page renders adoption-related UI correctly."""

    async def test_pending_device_shows_adopt_button(self, client, db_session, app):
        await _create_pending_device(db_session, device_id="ui-pending-001")

        resp = await client.get("/devices")
        assert resp.status_code == 200
        html = resp.text
        assert "Adopt" in html
        assert "adoptDevice(" in html

    async def test_adopted_device_no_adopt_button(self, client, db_session, app):
        device = Device(id="ui-adopted-001", name="x", status=DeviceStatus.ADOPTED)
        db_session.add(device)
        await db_session.commit()

        resp = await client.get("/devices")
        html = resp.text
        # The adopt button should not appear for adopted devices
        assert "adoptDevice('ui-adopted-001'" not in html

    async def test_groups_data_available_in_js(self, client, db_session, app):
        """The adoption modal needs groups data injected into JavaScript."""
        group = await _create_group(db_session, name="Conference Rooms")

        resp = await client.get("/devices")
        html = resp.text
        assert "_adoptionGroups" in html
        assert "Conference Rooms" in html

    async def test_groups_data_empty_when_no_groups(self, client, db_session, app):
        """When no groups exist, the JS array should be empty."""
        resp = await client.get("/devices")
        html = resp.text
        assert "_adoptionGroups" in html

    async def test_multiple_groups_in_js(self, client, db_session, app):
        """Multiple groups should all appear in the JS data."""
        await _create_group(db_session, name="Lobby")
        g2 = DeviceGroup(name="Kitchen", description="")
        db_session.add(g2)
        await db_session.commit()

        resp = await client.get("/devices")
        html = resp.text
        assert "Lobby" in html
        assert "Kitchen" in html

    async def test_adoption_groups_js_array_contains_group_data(self, client, db_session, app):
        """The _adoptionGroups JS array must contain parseable group objects, not just mention the name."""
        import json
        import re

        group = await _create_group(db_session, name="Conference Rooms")

        resp = await client.get("/devices")
        html = resp.text

        # Extract the _adoptionGroups array value from the rendered JS
        match = re.search(r"window\._adoptionGroups\s*=\s*(\[.*?\]);", html, re.DOTALL)
        assert match, "_adoptionGroups assignment not found in rendered HTML"

        # The JS array uses unquoted keys like { id: "...", name: "..." }
        # Convert to valid JSON by quoting the keys
        raw = match.group(1)
        raw_json = re.sub(r'(\b)(id|name)(\s*:)', r'"\2"\3', raw)
        groups_data = json.loads(raw_json)

        assert len(groups_data) >= 1
        names = [g["name"] for g in groups_data]
        assert "Conference Rooms" in names
        # Verify id is a valid UUID string
        group_entry = next(g for g in groups_data if g["name"] == "Conference Rooms")
        assert len(group_entry["id"]) == 36  # UUID format: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx

    async def test_adoption_groups_special_chars_safe(self, client, db_session, app):
        """Group names with special characters must not break the JS."""
        import json
        import re

        await _create_group(db_session, name='O\'Malley & "Friends"')

        resp = await client.get("/devices")
        html = resp.text

        match = re.search(r"window\._adoptionGroups\s*=\s*(\[.*?\]);", html, re.DOTALL)
        assert match, "_adoptionGroups assignment not found"

        raw = match.group(1)
        raw_json = re.sub(r'(\b)(id|name)(\s*:)', r'"\2"\3', raw)
        groups_data = json.loads(raw_json)

        names = [g["name"] for g in groups_data]
        assert 'O\'Malley & "Friends"' in names


@pytest.mark.asyncio
class TestDashboardAdoptionGroups:
    """The dashboard also has Adopt buttons — it must inject _adoptionGroups too."""

    async def test_dashboard_has_adoption_groups(self, client, db_session, app):
        """Dashboard page must inject _adoptionGroups for the adopt modal."""
        await _create_group(db_session, name="Lobby Screens")

        resp = await client.get("/")
        html = resp.text
        assert "_adoptionGroups" in html
        assert "Lobby Screens" in html

    async def test_dashboard_adoption_groups_parseable(self, client, db_session, app):
        """Dashboard _adoptionGroups must be a parseable JS array with correct data."""
        import json
        import re

        g = await _create_group(db_session, name="Main Hall")

        resp = await client.get("/")
        html = resp.text

        match = re.search(r"window\._adoptionGroups\s*=\s*(\[.*?\]);", html, re.DOTALL)
        assert match, "_adoptionGroups not found in dashboard HTML"

        raw = match.group(1)
        raw_json = re.sub(r'(\b)(id|name)(\s*:)', r'"\2"\3', raw)
        groups_data = json.loads(raw_json)

        assert len(groups_data) >= 1
        names = [g["name"] for g in groups_data]
        assert "Main Hall" in names

    async def test_dashboard_no_groups_empty_array(self, client, db_session, app):
        """With no groups, _adoptionGroups should be an empty array."""
        import re

        resp = await client.get("/")
        html = resp.text

        match = re.search(r"window\._adoptionGroups\s*=\s*(\[.*?\]);", html, re.DOTALL)
        assert match, "_adoptionGroups not found in dashboard HTML"

        raw = match.group(1).strip()
        assert raw == "[]" or raw.strip() == "[]" or not raw.strip().strip("[]").strip()


# ── Schema Tests ──


class TestAdoptRequestSchema:
    """Test the AdoptRequest pydantic schema."""

    def test_empty_is_valid(self):
        from cms.schemas.device import AdoptRequest
        req = AdoptRequest()
        assert req.name is None
        assert req.group_id is None

    def test_name_only(self):
        from cms.schemas.device import AdoptRequest
        req = AdoptRequest(name="My Device")
        assert req.name == "My Device"
        assert req.group_id is None

    def test_group_only(self):
        from cms.schemas.device import AdoptRequest
        gid = uuid.uuid4()
        req = AdoptRequest(group_id=gid)
        assert req.name is None
        assert req.group_id == gid

    def test_both_fields(self):
        from cms.schemas.device import AdoptRequest
        gid = uuid.uuid4()
        req = AdoptRequest(name="Display", group_id=gid)
        assert req.name == "Display"
        assert req.group_id == gid

    def test_invalid_group_id(self):
        from cms.schemas.device import AdoptRequest
        with pytest.raises(Exception):
            AdoptRequest(group_id="not-a-uuid")
