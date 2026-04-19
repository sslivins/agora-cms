"""Tests for devices page live-update revamp.

Verifies:
- DeviceOut schema includes new live-state fields
- API responses include cpu_temp_c, ip_address, ssh_enabled, etc.
- Firmware badge is permission-gated in the UI
- Status badges and live fields render correctly
- Online-only sections are always in the DOM (hidden when offline)
- __canManage JS variable is correctly set per role
"""

import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from cms.schemas.device import DeviceOut
from cms.models.device import DeviceStatus


# ── Schema tests ──


class TestDeviceOutSchema:
    """Verify DeviceOut includes all live-state fields."""

    def test_cpu_temp_field_exists(self):
        fields = DeviceOut.model_fields
        assert "cpu_temp_c" in fields

    def test_ip_address_field_exists(self):
        fields = DeviceOut.model_fields
        assert "ip_address" in fields

    def test_ssh_enabled_field_exists(self):
        fields = DeviceOut.model_fields
        assert "ssh_enabled" in fields

    def test_local_api_enabled_field_exists(self):
        fields = DeviceOut.model_fields
        assert "local_api_enabled" in fields

    def test_error_field_exists(self):
        fields = DeviceOut.model_fields
        assert "error" in fields

    def test_update_available_field_exists(self):
        fields = DeviceOut.model_fields
        assert "update_available" in fields

    def test_uptime_seconds_field_exists(self):
        fields = DeviceOut.model_fields
        assert "uptime_seconds" in fields

    def test_defaults_are_none_or_false(self):
        d = DeviceOut(
            id="test",
            name="test",
            status=DeviceStatus.ADOPTED,
            firmware_version="1.0.0",
            storage_capacity_mb=1000,
            storage_used_mb=100,
            registered_at="2026-01-01T00:00:00Z",
        )
        assert d.cpu_temp_c is None
        assert d.ip_address is None
        assert d.ssh_enabled is None
        assert d.local_api_enabled is None
        assert d.error is None
        assert d.update_available is False
        assert d.uptime_seconds == 0

    def test_schema_serializes_all_fields(self):
        d = DeviceOut(
            id="test",
            name="test",
            status=DeviceStatus.ADOPTED,
            firmware_version="1.0.0",
            storage_capacity_mb=1000,
            storage_used_mb=100,
            registered_at="2026-01-01T00:00:00Z",
            cpu_temp_c=65.3,
            ip_address="192.168.1.100",
            ssh_enabled=True,
            local_api_enabled=False,
            error="test error",
            update_available=True,
            uptime_seconds=3600,
        )
        data = d.model_dump()
        assert data["cpu_temp_c"] == 65.3
        assert data["ip_address"] == "192.168.1.100"
        assert data["ssh_enabled"] is True
        assert data["local_api_enabled"] is False
        assert data["error"] == "test error"
        assert data["update_available"] is True
        assert data["uptime_seconds"] == 3600


# ── API tests ──


@pytest_asyncio.fixture
async def device_with_live_state(app):
    """Create a device and register it in device_manager with live state."""
    from cms.database import get_db
    from cms.models.device import Device, DeviceStatus
    from cms.services.device_manager import device_manager
    from unittest.mock import AsyncMock

    factory = app.dependency_overrides[get_db]
    device_id = "live-test-device-001"

    async for db in factory():
        device = Device(
            id=device_id,
            status=DeviceStatus.ADOPTED,
            name="Live Test Device",
            firmware_version="1.0.0",
        )
        db.add(device)
        await db.commit()
        break

    # Register a mock WebSocket connection
    mock_ws = AsyncMock()
    conn = device_manager.register(device_id, mock_ws, ip_address="10.0.0.42")

    # Update with live status
    device_manager.update_status(
        device_id,
        mode="playback",
        asset="test-video.mp4",
        uptime_seconds=7200,
        cpu_temp_c=72.5,
        pipeline_state="PLAYING",
        ssh_enabled=True,
        local_api_enabled=False,
        display_connected=True,
    )

    yield device_id

    # Cleanup
    device_manager.disconnect(device_id)


@pytest.mark.asyncio
class TestDeviceAPILiveFields:
    """Verify that the API returns live-state fields."""

    async def test_list_devices_includes_live_fields(self, client, device_with_live_state):
        resp = await client.get("/api/devices")
        assert resp.status_code == 200
        devices = resp.json()
        d = next(dev for dev in devices if dev["id"] == device_with_live_state)

        assert d["cpu_temp_c"] == 72.5
        assert d["ip_address"] == "10.0.0.42"
        assert d["ssh_enabled"] is True
        assert d["local_api_enabled"] is False
        assert d["is_online"] is True
        assert d["uptime_seconds"] == 7200
        assert d["pipeline_state"] == "PLAYING"
        assert d["display_connected"] is True
        assert d["playback_asset"] == "test-video.mp4"

    async def test_get_device_includes_live_fields(self, client, device_with_live_state):
        resp = await client.get(f"/api/devices/{device_with_live_state}")
        assert resp.status_code == 200
        d = resp.json()

        assert d["cpu_temp_c"] == 72.5
        assert d["ip_address"] == "10.0.0.42"
        assert d["ssh_enabled"] is True
        assert d["local_api_enabled"] is False

    async def test_offline_device_has_null_live_fields(self, client, app):
        """A device not in device_manager should have null live fields."""
        from cms.database import get_db
        from cms.models.device import Device, DeviceStatus

        factory = app.dependency_overrides[get_db]
        device_id = "offline-test-device-001"

        async for db in factory():
            device = Device(
                id=device_id,
                status=DeviceStatus.ADOPTED,
                name="Offline Device",
                firmware_version="1.0.0",
            )
            db.add(device)
            await db.commit()
            break

        resp = await client.get(f"/api/devices/{device_id}")
        assert resp.status_code == 200
        d = resp.json()

        assert d["cpu_temp_c"] is None
        assert d["ip_address"] is None
        assert d["ssh_enabled"] is None
        assert d["local_api_enabled"] is None
        assert d["is_online"] is False
        assert d["uptime_seconds"] == 0


# ── UI rendering tests ──


@pytest_asyncio.fixture
async def operator_client(app):
    """Authenticated HTTP client logged in as an operator user."""
    from sqlalchemy import select
    from cms.database import get_db
    from cms.models.user import Role, User
    from cms.auth import hash_password

    factory = app.dependency_overrides[get_db]

    async for db in factory():
        result = await db.execute(select(Role).where(Role.name == "Operator"))
        op_role = result.scalar_one()

        op_user = User(
            username="operator_live_test",
            email="operator_live@test.com",
            display_name="Test Operator Live",
            password_hash=hash_password("operatorpass"),
            role_id=op_role.id,
            is_active=True,
            must_change_password=False,
        )
        db.add(op_user)
        await db.commit()
        break

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        await ac.post("/login", data={"username": "operator_live_test", "password": "operatorpass"}, follow_redirects=False)
        yield ac


@pytest_asyncio.fixture
async def viewer_client(app):
    """Authenticated HTTP client logged in as a viewer user."""
    from sqlalchemy import select
    from cms.database import get_db
    from cms.models.user import Role, User
    from cms.auth import hash_password

    factory = app.dependency_overrides[get_db]

    async for db in factory():
        result = await db.execute(select(Role).where(Role.name == "Viewer"))
        viewer_role = result.scalar_one()

        viewer_user = User(
            username="viewer_live_test",
            email="viewer_live@test.com",
            display_name="Test Viewer Live",
            password_hash=hash_password("viewerpass"),
            role_id=viewer_role.id,
            is_active=True,
            must_change_password=False,
        )
        db.add(viewer_user)
        await db.commit()
        break

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        await ac.post("/login", data={"username": "viewer_live_test", "password": "viewerpass"}, follow_redirects=False)
        yield ac


@pytest.mark.asyncio
class TestDevicesUILiveUpdates:
    """Verify UI rendering for live-update elements."""

    async def test_admin_has_can_manage_true(self, client, device_with_live_state):
        resp = await client.get("/devices")
        assert resp.status_code == 200
        assert "__canManage = true" in resp.text

    async def test_operator_has_can_manage_false(self, operator_client, device_with_live_state):
        resp = await operator_client.get("/devices")
        assert resp.status_code == 200
        assert "__canManage = false" in resp.text

    async def test_data_live_status_attr_in_html(self, client, device_with_live_state):
        """The status badge cell should have data-live-status attribute."""
        resp = await client.get("/devices")
        assert resp.status_code == 200
        assert f'data-live-status="{device_with_live_state}"' in resp.text

    async def test_data_live_temp_attr_in_html(self, client, device_with_live_state):
        """CPU temp span should have data-live-temp attribute."""
        resp = await client.get("/devices")
        assert resp.status_code == 200
        assert f'data-live-temp="{device_with_live_state}"' in resp.text

    async def test_data_live_ip_attr_in_html(self, client, device_with_live_state):
        """IP address span should have data-live-ip attribute."""
        resp = await client.get("/devices")
        assert resp.status_code == 200
        assert f'data-live-ip="{device_with_live_state}"' in resp.text

    async def test_data_live_storage_attr_in_html(self, client, device_with_live_state):
        """Storage detail span should have data-live-storage attribute."""
        resp = await client.get("/devices")
        assert resp.status_code == 200
        assert f'data-live-storage="{device_with_live_state}"' in resp.text

    async def test_data_live_actions_attr_in_html(self, client, device_with_live_state):
        """Actions cell should have data-live-actions attribute."""
        resp = await client.get("/devices")
        assert resp.status_code == 200
        assert f'data-live-actions="{device_with_live_state}"' in resp.text

    async def test_online_section_visible_for_online_device(self, client, device_with_live_state):
        """Online detail fields should be visible (no display:none) for an online device."""
        resp = await client.get("/devices")
        assert resp.status_code == 200
        # The online section should NOT have display:none when device is online
        text = resp.text
        idx = text.find(f'data-live-online-section="{device_with_live_state}"')
        assert idx > -1
        # Check the surrounding context doesn't have display:none
        context = text[max(0, idx - 100):idx + 100]
        assert 'display:none' not in context

    async def test_ssh_button_visible_for_admin(self, client, device_with_live_state):
        """SSH toggle button wrapper should exist with data-live-ssh-btn."""
        resp = await client.get("/devices")
        assert resp.status_code == 200
        assert f'data-live-ssh-btn="{device_with_live_state}"' in resp.text

    async def test_toolbar_hidden_for_operator(self, operator_client, device_with_live_state):
        """Operator should NOT see the detail toolbar (manage actions)."""
        resp = await operator_client.get("/devices")
        assert resp.status_code == 200
        assert f'data-live-toolbar="{device_with_live_state}"' not in resp.text

    async def test_temp_displays_with_value(self, client, device_with_live_state):
        """Device with cpu_temp_c=72.5 should show temperature in the HTML."""
        resp = await client.get("/devices")
        assert resp.status_code == 200
        assert "72.5°C" in resp.text

    async def test_ip_displays_in_detail(self, client, device_with_live_state):
        """Device with ip_address should show it in the detail panel."""
        resp = await client.get("/devices")
        assert resp.status_code == 200
        assert "10.0.0.42" in resp.text

    async def test_display_connected_badge_rendered_on_initial_page_load(self, client, device_with_live_state):
        """Regression: /devices should render the Connected badge on first render
        (not wait for a WebSocket push). The fixture sets display_connected=True,
        so the detail panel must show 'Connected' — not 'Disconnected' or '—'.
        Prior to the fix, d.display_connected was never populated from live_states
        on the server-side template context, so Jinja saw Undefined and fell
        through to the 'Disconnected' else-branch until the WS heartbeat arrived.
        """
        resp = await client.get("/devices")
        assert resp.status_code == 200
        text = resp.text
        idx = text.find(f'data-live-display="{device_with_live_state}"')
        assert idx > -1
        # Slice the ~300 chars after the anchor — the badge is within that span.
        snippet = text[idx:idx + 300]
        assert 'badge-online">Connected<' in snippet, (
            "Expected Connected badge on initial render; got:\n" + snippet
        )
        assert 'Disconnected' not in snippet


@pytest.mark.asyncio
class TestFirmwareBadgePermission:
    """Firmware update badge should only show for users with devices:manage."""

    async def test_admin_sees_firmware_badge(self, client, app):
        """Admin should see firmware update badge when update is available."""
        from cms.database import get_db
        from cms.models.device import Device, DeviceStatus
        from cms.services import version_checker

        # Set a known latest version
        version_checker._latest_version = "99.0.0"

        factory = app.dependency_overrides[get_db]
        device_id = "firmware-test-001"

        async for db in factory():
            device = Device(
                id=device_id,
                status=DeviceStatus.ADOPTED,
                name="Firmware Test",
                firmware_version="1.0.0",
            )
            db.add(device)
            await db.commit()
            break

        resp = await client.get("/devices")
        assert resp.status_code == 200
        assert "99.0.0 available" in resp.text

        # Cleanup
        version_checker._latest_version = None

    async def test_operator_no_firmware_badge(self, operator_client, app):
        """Operator should NOT see firmware update badge."""
        from cms.database import get_db
        from cms.models.device import Device, DeviceStatus
        from cms.services import version_checker

        version_checker._latest_version = "99.0.0"

        factory = app.dependency_overrides[get_db]
        device_id = "firmware-test-002"

        async for db in factory():
            device = Device(
                id=device_id,
                status=DeviceStatus.ADOPTED,
                name="Firmware Test Op",
                firmware_version="1.0.0",
            )
            db.add(device)
            await db.commit()
            break

        resp = await operator_client.get("/devices")
        assert resp.status_code == 200
        assert "99.0.0 available" not in resp.text

        version_checker._latest_version = None


@pytest.mark.asyncio
class TestDeviceStatusBadgeRendering:
    """Verify status badge data attributes are correctly set."""

    async def test_adopted_online_shows_online_badge(self, client, device_with_live_state):
        resp = await client.get("/devices")
        assert resp.status_code == 200
        # Device is online → should have "Online" badge
        text = resp.text
        idx = text.find(f'data-live-status="{device_with_live_state}"')
        assert idx > -1
        context = text[idx:idx + 200]
        assert "badge-online" in context
        assert "Online" in context

    async def test_pending_device_shows_pending_badge(self, client, app):
        from cms.database import get_db
        from cms.models.device import Device, DeviceStatus

        factory = app.dependency_overrides[get_db]
        device_id = "pending-badge-test-001"

        async for db in factory():
            device = Device(
                id=device_id,
                status=DeviceStatus.PENDING,
                name="Pending Badge Test",
            )
            db.add(device)
            await db.commit()
            break

        resp = await client.get("/devices")
        assert resp.status_code == 200
        text = resp.text
        idx = text.find(f'data-live-status="{device_id}"')
        assert idx > -1
        context = text[idx:idx + 300]
        assert "Pending" in context

    async def test_data_device_status_attribute(self, client, device_with_live_state):
        """Each status cell should have data-device-status with the device status value."""
        resp = await client.get("/devices")
        assert resp.status_code == 200
        assert 'data-device-status="adopted"' in resp.text


@pytest.mark.asyncio
class TestStorageFormatting:
    """Verify storage is displayed in human-readable units."""

    async def test_large_storage_shows_gb(self, client, app):
        """Storage >= 1024 MB should display as GB."""
        from cms.database import get_db
        from cms.models.device import Device, DeviceStatus

        factory = app.dependency_overrides[get_db]
        device_id = "storage-gb-test-001"

        async for db in factory():
            device = Device(
                id=device_id,
                status=DeviceStatus.ADOPTED,
                name="Large Storage Device",
                firmware_version="1.0.0",
                storage_capacity_mb=128000,
                storage_used_mb=64000,
            )
            db.add(device)
            await db.commit()
            break

        resp = await client.get("/devices")
        assert resp.status_code == 200
        # Should show GB, not raw MB
        assert "62.5 GB" in resp.text  # 64000 / 1024
        assert "125.0 GB" in resp.text  # 128000 / 1024

    async def test_small_storage_shows_mb(self, client, app):
        """Storage < 1024 MB should display as MB."""
        from cms.database import get_db
        from cms.models.device import Device, DeviceStatus

        factory = app.dependency_overrides[get_db]
        device_id = "storage-mb-test-001"

        async for db in factory():
            device = Device(
                id=device_id,
                status=DeviceStatus.ADOPTED,
                name="Small Storage Device",
                firmware_version="1.0.0",
                storage_capacity_mb=512,
                storage_used_mb=100,
            )
            db.add(device)
            await db.commit()
            break

        resp = await client.get("/devices")
        assert resp.status_code == 200
        assert "100 MB" in resp.text
        assert "512 MB" in resp.text

    async def test_js_fmtStorage_function_present(self, client, device_with_live_state):
        """The fmtStorage JS helper should be in the page for live updates."""
        resp = await client.get("/devices")
        assert resp.status_code == 200
        assert "function fmtStorage" in resp.text


@pytest.mark.asyncio
class TestSelectDropdownStyling:
    """Verify all select elements get the global dropdown chevron and styling."""

    async def test_global_select_chevron_in_css(self, client):
        """Global style.css must contain the select chevron background-image."""
        resp = await client.get("/static/style.css")
        assert resp.status_code == 200
        css = resp.text
        # The global select rule must have appearance:none and the SVG chevron
        assert "appearance: none" in css
        assert "background-image:" in css
        assert "M0 0l5 6 5-6z" in css  # the chevron SVG path

    async def test_no_background_shorthand_on_inline_edit(self, client):
        """inline-edit must use background-color (not background shorthand)
        so the global chevron background-image is not overridden."""
        resp = await client.get("/static/style.css")
        assert resp.status_code == 200
        css = resp.text
        # Find the .inline-edit rule — it should NOT use 'background:' shorthand
        import re
        inline_rules = re.findall(r'\.inline-edit\s*\{[^}]+\}', css)
        for rule in inline_rules:
            # Allow 'background-color' and 'background-image' but not bare 'background:'
            stripped = re.sub(r'background-(color|image|repeat|position|size)', '', rule)
            assert 'background:' not in stripped, (
                f".inline-edit uses 'background:' shorthand which kills the chevron: {rule}"
            )

    async def test_no_background_shorthand_on_form_group_select(self, client):
        """form-group inputs must use background-color not background shorthand."""
        resp = await client.get("/static/style.css")
        assert resp.status_code == 200
        css = resp.text
        import re
        # Match multiline rules like .form-group input,\n.form-group select { ... }
        form_rules = re.findall(
            r'\.form-group\s+(?:input|select|textarea)[\s\S]*?\{([^}]+)\}', css
        )
        for rule_body in form_rules:
            stripped = re.sub(r'background-(color|image|repeat|position|size)', '', rule_body)
            assert 'background:' not in stripped, (
                f"form-group uses 'background:' shorthand which kills the chevron: {rule_body}"
            )

    async def test_select_vars_defined_in_root(self, client):
        """CSS must define --select-bg and --select-border variables."""
        resp = await client.get("/static/style.css")
        assert resp.status_code == 200
        assert "--select-bg:" in resp.text
        assert "--select-border:" in resp.text

    async def test_device_selects_have_inline_edit_class(self, client, device_with_live_state):
        """Device page selects should have inline-edit class for consistent styling."""
        resp = await client.get("/devices")
        assert resp.status_code == 200
        assert 'class="inline-edit"' in resp.text
