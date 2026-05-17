"""Issue #583: disabled profiles must not be selectable for devices.

Surfaces tested:
  * Per-device profile dropdown in the ``device_row`` macro (devices page) —
    must exclude disabled profiles UNLESS one is currently assigned to that
    device (in which case it renders selected with a "(disabled)" suffix so
    the operator can see WHY the device is in a weird state and pick an
    enabled profile to replace it).
  * Adoption modals — the ``window._adoptionProfiles`` JS array on both
    ``/devices`` and ``/dashboard`` only contains enabled profiles.
"""

import uuid

import pytest


@pytest.mark.asyncio
class TestDeviceDropdownFilter:
    async def _make_profile(self, db_session, name, *, enabled=True):
        from cms.models.device_profile import DeviceProfile
        p = DeviceProfile(name=name, video_codec="h264", enabled=enabled)
        db_session.add(p)
        await db_session.flush()
        return p

    async def _make_device(self, db_session, name, *, profile_id=None):
        from cms.models.device import Device, DeviceStatus
        d = Device(
            id=f"{name}-{uuid.uuid4().hex[:6]}",
            name=name,
            status=DeviceStatus.ADOPTED,
            profile_id=profile_id,
        )
        db_session.add(d)
        await db_session.flush()
        return d

    def _option_for(self, html: str, profile_id) -> str | None:
        """Return the substring of html containing the <option> for the
        given profile_id, or None if no such option exists."""
        marker = f'value="{profile_id}"'
        idx = html.find(marker)
        if idx == -1:
            return None
        start = html.rfind("<option", 0, idx)
        end = html.find("</option>", idx)
        return html[start:end + len("</option>")]

    async def test_dropdown_excludes_disabled_profile(self, client, db_session):
        """When no device is assigned to it, a disabled profile must not
        appear as an <option> in any per-device dropdown."""
        on = await self._make_profile(db_session, "ddf-on")
        off = await self._make_profile(db_session, "ddf-off", enabled=False)
        # Make a device assigned to the ENABLED profile so the page has a row.
        await self._make_device(db_session, "ddf-dev", profile_id=on.id)
        await db_session.commit()

        resp = await client.get("/devices")
        assert resp.status_code == 200
        body = resp.text
        assert self._option_for(body, on.id) is not None
        assert self._option_for(body, off.id) is None, (
            "disabled profile (with no device assigned to it) must not appear "
            "in any per-device profile dropdown"
        )

    async def test_dropdown_keeps_currently_assigned_disabled_profile(
        self, client, db_session,
    ):
        """A device assigned to profile P keeps showing P in its dropdown
        (with a "(disabled)" suffix and selected) even after P is disabled —
        otherwise the dropdown would render no selected option at all."""
        p = await self._make_profile(db_session, "ddf-keep")
        d = await self._make_device(db_session, "ddf-keep-dev", profile_id=p.id)
        await db_session.commit()
        pid = p.id
        did = d.id

        # Disable the profile through the API (simulates the real flow).
        r = await client.post(f"/api/profiles/{pid}/disable")
        assert r.status_code == 200

        resp = await client.get("/devices")
        assert resp.status_code == 200
        body = resp.text
        opt = self._option_for(body, pid)
        assert opt is not None, (
            f"profile {pid} currently assigned to device {did} must still appear "
            "in that device's dropdown even when disabled"
        )
        assert "selected" in opt
        assert "(disabled)" in opt

    async def test_dropdown_no_profile_assigned_shows_only_enabled(
        self, client, db_session,
    ):
        """NULL-safety: when ``device.profile_id`` is None the Jinja
        ``p.enabled or d.profile_id == p.id`` guard must still exclude
        disabled profiles (the second clause is False for every profile)."""
        on = await self._make_profile(db_session, "ddf-null-on")
        off = await self._make_profile(db_session, "ddf-null-off", enabled=False)
        await self._make_device(db_session, "ddf-null-dev", profile_id=None)
        await db_session.commit()

        resp = await client.get("/devices")
        assert resp.status_code == 200
        body = resp.text
        assert self._option_for(body, on.id) is not None
        assert self._option_for(body, off.id) is None

    async def test_devices_page_adoption_array_excludes_disabled(
        self, client, db_session,
    ):
        """``window._adoptionProfiles`` on /devices is the JS source the
        adoption modal reads — disabled profiles must not appear."""
        on = await self._make_profile(db_session, "ddf-mod-on")
        off = await self._make_profile(db_session, "ddf-mod-off", enabled=False)
        await db_session.commit()

        resp = await client.get("/devices")
        assert resp.status_code == 200
        # Extract the _adoptionProfiles literal.
        body = resp.text
        marker = "window._adoptionProfiles ="
        idx = body.find(marker)
        assert idx != -1, "expected window._adoptionProfiles in /devices HTML"
        end = body.find("];", idx)
        assert end != -1
        snippet = body[idx:end]
        assert "ddf-mod-on" in snippet
        assert "ddf-mod-off" not in snippet, (
            "disabled profile must not appear in the adoption modal's source array"
        )
        # Sanity: the disabled profile's id must not appear either.
        assert str(off.id) not in snippet

    async def test_dashboard_adoption_array_excludes_disabled(
        self, client, db_session,
    ):
        """Same check for the dashboard's adoption modal (separate query
        in cms/ui.py). Dashboard is served at '/'."""
        on = await self._make_profile(db_session, "ddf-dash-on")
        off = await self._make_profile(db_session, "ddf-dash-off", enabled=False)
        await db_session.commit()

        resp = await client.get("/")
        assert resp.status_code == 200
        body = resp.text
        marker = "window._adoptionProfiles ="
        idx = body.find(marker)
        assert idx != -1, "expected window._adoptionProfiles in dashboard HTML"
        end = body.find("];", idx)
        assert end != -1
        snippet = body[idx:end]
        assert "ddf-dash-on" in snippet
        assert "ddf-dash-off" not in snippet
        assert str(off.id) not in snippet
