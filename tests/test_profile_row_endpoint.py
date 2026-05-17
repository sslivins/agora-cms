"""Unit test for GET /api/profiles/{id}/row — the HTML fragment endpoint
used by the profiles page's per-row poller to swap a single <tr> without
a full page reload (issue #87)."""

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from cms.models.device_profile import DeviceProfile


@pytest_asyncio.fixture
async def viewer_client(app):
    """Authenticated HTTP client logged in as a viewer (read-only) user."""
    from sqlalchemy import select
    from cms.database import get_db
    from cms.models.user import Role, User
    from cms.auth import hash_password

    factory = app.dependency_overrides[get_db]
    async for db in factory():
        result = await db.execute(select(Role).where(Role.name == "Viewer"))
        viewer_role = result.scalar_one()
        viewer_user = User(
            username="viewer_profile_row_test",
            email="viewer_profile_row@test.com",
            display_name="Viewer Profile Row",
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
        await ac.post(
            "/login",
            data={"username": "viewer_profile_row_test", "password": "viewerpass"},
            follow_redirects=False,
        )
        yield ac


@pytest.mark.asyncio
class TestProfileRowEndpoint:
    async def _make_profile(
        self, db_session, name="row-test", *, builtin=False, enabled=True
    ) -> DeviceProfile:
        profile = DeviceProfile(
            name=name,
            description="row endpoint test",
            video_codec="h264",
            video_profile="main",
            max_width=1920,
            max_height=1080,
            max_fps=30,
            crf=23,
            video_bitrate="",
            pixel_format="auto",
            color_space="auto",
            audio_codec="aac",
            audio_bitrate="128k",
            builtin=builtin,
            enabled=enabled,
        )
        db_session.add(profile)
        await db_session.commit()
        await db_session.refresh(profile)
        return profile

    async def test_row_returns_html_fragment(self, client, db_session):
        profile = await self._make_profile(db_session)
        resp = await client.get(f"/api/profiles/{profile.id}/row")
        assert resp.status_code == 200
        body = resp.text
        assert f'data-profile-id="{profile.id}"' in body
        assert "row-test" in body

    async def test_row_unknown_id_404(self, client):
        resp = await client.get("/api/profiles/00000000-0000-0000-0000-000000000000/row")
        assert resp.status_code == 404

    # ── Issue #583 row rendering ──

    async def test_enabled_profile_row_shows_disable_item(self, client, db_session):
        profile = await self._make_profile(db_session, name="enabled-row")
        resp = await client.get(f"/api/profiles/{profile.id}/row")
        assert resp.status_code == 200
        body = resp.text
        # Enabled profiles render the Disable kebab item, NOT Enable.
        assert "disableProfile(" in body
        assert "enableProfile(" not in body
        # No inactive styling or "disabled" badge for an enabled profile.
        assert "row-inactive" not in body
        assert ">disabled<" not in body

    async def test_disabled_profile_row_shows_enable_item(self, client, db_session):
        profile = await self._make_profile(db_session, name="disabled-row", enabled=False)
        resp = await client.get(f"/api/profiles/{profile.id}/row")
        assert resp.status_code == 200
        body = resp.text
        assert "enableProfile(" in body
        assert "disableProfile(" not in body
        assert "row-inactive" in body
        # Disabled badge in the name cell.
        assert ">disabled<" in body

    async def test_builtin_enabled_row_shows_disable_item(self, client, db_session):
        # Built-ins must be disable-able (issue #583's headline ask).
        profile = await self._make_profile(db_session, name="builtin-enabled", builtin=True)
        resp = await client.get(f"/api/profiles/{profile.id}/row")
        assert resp.status_code == 200
        body = resp.text
        assert "disableProfile(" in body

    async def test_builtin_disabled_row_shows_enable_item(self, client, db_session):
        profile = await self._make_profile(
            db_session, name="builtin-disabled", builtin=True, enabled=False
        )
        resp = await client.get(f"/api/profiles/{profile.id}/row")
        assert resp.status_code == 200
        body = resp.text
        assert "enableProfile(" in body
        assert "row-inactive" in body

    async def test_row_readonly_user_has_no_kebab(self, viewer_client, db_session):
        profile = await self._make_profile(db_session, name="viewer-row")
        resp = await viewer_client.get(f"/api/profiles/{profile.id}/row")
        assert resp.status_code == 200
        body = resp.text
        # Read-only users see no kebab menu and therefore no enable/disable items.
        assert "kebab-menu" not in body
        assert "disableProfile(" not in body
        assert "enableProfile(" not in body

