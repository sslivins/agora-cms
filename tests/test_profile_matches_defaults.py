"""Tests for the matches_defaults flag on ProfileOut (issue #262).

Verifies that the Reset button's disabled state is driven by a
server-computed boolean that reflects whether a built-in profile's
transcoding-relevant fields all still match the canonical factory
defaults (description-only changes do not count).
"""

import pytest

from cms.models.device_profile import DeviceProfile
from cms.profile_defaults import BUILTIN_PROFILES


@pytest.mark.asyncio
class TestMatchesDefaults:
    """matches_defaults appears on GET /api/profiles and /api/profiles/status."""

    async def _make_default_builtin(self, db_session, name: str = "pi-zero-2w") -> DeviceProfile:
        defaults = BUILTIN_PROFILES[name]
        profile = DeviceProfile(name=name, builtin=True, **defaults)
        db_session.add(profile)
        await db_session.commit()
        await db_session.refresh(profile)
        return profile

    async def test_unmodified_builtin_matches_defaults_true(self, client, db_session):
        await self._make_default_builtin(db_session)
        resp = await client.get("/api/profiles")
        assert resp.status_code == 200
        rows = [p for p in resp.json() if p["name"] == "pi-zero-2w"]
        assert len(rows) == 1
        assert rows[0]["matches_defaults"] is True

    async def test_transcode_field_edit_sets_matches_defaults_false(self, client, db_session):
        profile = await self._make_default_builtin(db_session)

        # Change a transcoding-relevant field
        resp = await client.put(
            f"/api/profiles/{profile.id}",
            json={"max_fps": 24},
        )
        assert resp.status_code == 200
        assert resp.json()["matches_defaults"] is False

        # Confirmed via list endpoint too
        rows = [p for p in (await client.get("/api/profiles")).json() if p["id"] == str(profile.id)]
        assert rows[0]["matches_defaults"] is False

    async def test_description_only_edit_keeps_matches_defaults_true(self, client, db_session):
        """AC #5 — editing only the description should NOT enable Reset."""
        profile = await self._make_default_builtin(db_session)

        resp = await client.put(
            f"/api/profiles/{profile.id}",
            json={"description": "my annotation"},
        )
        assert resp.status_code == 200
        assert resp.json()["matches_defaults"] is True

    async def test_non_builtin_matches_defaults_false(self, client, db_session):
        """Custom profiles never report matches_defaults=True."""
        profile = DeviceProfile(
            name="custom-profile",
            video_codec="h264",
            video_profile="main",
            builtin=False,
        )
        db_session.add(profile)
        await db_session.commit()

        rows = [p for p in (await client.get("/api/profiles")).json() if p["id"] == str(profile.id)]
        assert rows[0]["matches_defaults"] is False

    async def test_reset_restores_matches_defaults_true(self, client, db_session):
        """After Reset, the returned ProfileOut should report matches_defaults=True."""
        profile = await self._make_default_builtin(db_session)

        # Deviate, then reset
        await client.put(f"/api/profiles/{profile.id}", json={"max_fps": 24})
        resp = await client.post(f"/api/profiles/{profile.id}/reset")
        assert resp.status_code == 200
        assert resp.json()["matches_defaults"] is True

    async def test_status_endpoint_includes_matches_defaults(self, client, db_session):
        """Live-poll endpoint carries the same flag so JS can toggle the button."""
        profile = await self._make_default_builtin(db_session)

        resp = await client.get("/api/profiles/status")
        assert resp.status_code == 200
        entry = next(p for p in resp.json()["profiles"] if p["id"] == str(profile.id))
        assert entry["matches_defaults"] is True

        await client.put(f"/api/profiles/{profile.id}", json={"crf": 30})
        resp = await client.get("/api/profiles/status")
        entry = next(p for p in resp.json()["profiles"] if p["id"] == str(profile.id))
        assert entry["matches_defaults"] is False
