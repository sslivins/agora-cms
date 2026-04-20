"""Unit test for GET /api/profiles/{id}/row — the HTML fragment endpoint
used by the profiles page's per-row poller to swap a single <tr> without
a full page reload (issue #87)."""

import pytest

from cms.models.device_profile import DeviceProfile


@pytest.mark.asyncio
class TestProfileRowEndpoint:
    async def _make_profile(self, db_session, name="row-test") -> DeviceProfile:
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
            builtin=False,
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
