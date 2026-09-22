"""Tests for the voice-announcement API and builder routes."""

from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest
from sqlalchemy import select

from cms.models.asset import Asset, AssetType
from cms.models.voice_announcement import VoiceAnnouncement
from shared.models.job import Job, JobOutbox, JobStatus, JobType


def _payload(**overrides):
    base = {
        "display_name": "Lobby welcome",
        "script_text": "Welcome to the lobby.",
        "voice_name": "en-US-Ava:MAI-Voice-2",
        "emotion": "cheerful",
        "language": "en-US",
        "speech_rate": "+15%",
    }
    base.update(overrides)
    return base


class _PreviewSpeechClient:
    def __init__(self, _settings):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return None

    async def synthesize(self, script_text, **kwargs):
        assert script_text == "Welcome to the lobby."
        assert kwargs["voice_name"] == "en-US-Ava:MAI-Voice-2"
        assert kwargs["emotion"] == "cheerful"
        return b"fake-ogg"


class _VoiceListSpeechClient:
    def __init__(self, _settings):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return None

    async def list_voices(self, language=None):
        assert language == "en-US"
        return [
            {
                "short_name": "en-US-Ava:MAI-Voice-2",
                "display_name": "Ava",
                "locale": "en-US",
                "emotions": ["cheerful", "sad"],
            }
        ]


class _MultiLocaleSpeechClient:
    """Records the scoping argument the router actually passes through.

    The builder derives its Language dropdown from the locales present in
    an unscoped catalogue, so the endpoint defaulting to ``None`` rather
    than ``en-US`` is load-bearing: a default of en-US would collapse the
    dropdown to a single entry and hide every other language.
    """

    seen_language: object = "<unset>"

    def __init__(self, _settings):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return None

    async def list_voices(self, language=None):
        type(self).seen_language = language
        catalogue = [
            {"short_name": "en-US-Ava:MAI-Voice-2", "display_name": "Ava",
             "locale": "en-US", "emotions": []},
            {"short_name": "fr-FR-Marc:MAI-Voice-2", "display_name": "Marc",
             "locale": "fr-FR", "emotions": []},
            {"short_name": "hu-HU-Lilla:MAI-Voice-2", "display_name": "Lilla",
             "locale": "hu-HU", "emotions": []},
        ]
        if not language:
            return catalogue
        return [v for v in catalogue if v["locale"].startswith(language)]


async def _seed_voice_asset(db_session, *, owner_id=None):
    asset_id = uuid.uuid4()
    asset = Asset(
        id=asset_id,
        filename=f"{asset_id}.ogg",
        display_name="Existing announcement",
        asset_type=AssetType.VOICE_ANNOUNCEMENT,
        size_bytes=123,
        checksum="abc123",
        audio_codec="opus",
        uploaded_by_user_id=owner_id,
        is_global=True,
    )
    voice = VoiceAnnouncement(
        asset_id=asset_id,
        script_text="Original script",
        voice_name="en-US-Ava:MAI-Voice-2",
        emotion=None,
        language="en-US",
        speech_rate=None,
        generation_status=JobStatus.DONE,
    )
    db_session.add_all([asset, voice])
    await db_session.commit()
    return asset, voice


@pytest.mark.asyncio
class TestVoiceAnnouncementsApi:
    async def test_create_voice_announcement_enqueues_job(self, client, db_session):
        with patch("cms.routers.voice_announcements.is_available", return_value=True):
            resp = await client.post("/api/voice-announcements", json=_payload())

        assert resp.status_code == 201, resp.text
        body = resp.json()
        asset_id = uuid.UUID(body["asset_id"])
        assert body["generation_status"] == JobStatus.PENDING.value
        assert body["edit_url"] == f"/assets/{asset_id}/voice"

        asset = (await db_session.execute(select(Asset).where(Asset.id == asset_id))).scalar_one()
        voice = (
            await db_session.execute(
                select(VoiceAnnouncement).where(VoiceAnnouncement.asset_id == asset_id)
            )
        ).scalar_one()
        job = (
            await db_session.execute(
                select(Job).where(
                    Job.target_id == asset_id,
                    Job.type == JobType.VOICE_SYNTHESIS,
                )
            )
        ).scalar_one()
        outbox = (
            await db_session.execute(select(JobOutbox).where(JobOutbox.job_id == job.id))
        ).scalar_one()

        assert asset.asset_type == AssetType.VOICE_ANNOUNCEMENT
        assert asset.filename == f"{asset_id}.ogg"
        assert asset.display_name == "Lobby welcome"
        assert voice.script_text == "Welcome to the lobby."
        assert voice.generation_status == JobStatus.PENDING
        assert job.status == JobStatus.PENDING
        assert outbox.job_id == job.id

    async def test_get_status_returns_generation_fields(self, client, db_session):
        asset, voice = await _seed_voice_asset(db_session)
        voice.generation_status = JobStatus.FAILED
        voice.generation_error = "Bad SSML"
        await db_session.commit()

        resp = await client.get(f"/api/voice-announcements/{asset.id}")
        assert resp.status_code == 200, resp.text
        assert resp.json() == {
            "asset_id": str(asset.id),
            "generation_status": "failed",
            "generation_error": "Bad SSML",
            "last_generated_at": None,
        }

    async def test_preview_returns_audio_blob(self, client):
        with patch("cms.routers.voice_announcements.SpeechClient", _PreviewSpeechClient):
            resp = await client.post(
                "/api/voice-announcements/preview",
                json=_payload(),
            )

        assert resp.status_code == 200, resp.text
        assert resp.content == b"fake-ogg"
        assert resp.headers["content-type"].startswith("audio/ogg")

    async def test_update_resets_generation_and_enqueues_job(self, client, db_session):
        asset, voice = await _seed_voice_asset(db_session)
        with patch("cms.routers.voice_announcements.is_available", return_value=True):
            resp = await client.put(
                f"/api/voice-announcements/{asset.id}",
                json=_payload(
                    display_name="Updated announcement",
                    script_text="Updated script",
                    emotion=None,
                    speech_rate=None,
                ),
            )

        assert resp.status_code == 200, resp.text
        assert resp.json()["generation_status"] == "pending"

        await db_session.refresh(asset)
        await db_session.refresh(voice)
        job = (
            await db_session.execute(
                select(Job)
                .where(Job.target_id == asset.id, Job.type == JobType.VOICE_SYNTHESIS)
                .order_by(Job.created_at.desc())
            )
        ).scalars().first()

        assert asset.display_name == "Updated announcement"
        assert asset.size_bytes == 0
        assert asset.checksum == ""
        assert asset.audio_codec is None
        assert voice.script_text == "Updated script"
        assert voice.emotion is None
        assert voice.speech_rate is None
        assert voice.generation_status == JobStatus.PENDING
        assert job is not None

    async def test_mutations_require_assets_write(self, app, db_session):
        from tests.test_ui_overhaul import _create_user, _login_as

        await _create_user(db_session, username="voice_viewer", role_name="Viewer")
        ac = await _login_as(app, "voice_viewer")
        try:
            create_resp = await ac.post("/api/voice-announcements", json=_payload())
            preview_resp = await ac.post("/api/voice-announcements/preview", json=_payload())
            assert create_resp.status_code == 403
            assert preview_resp.status_code == 403
        finally:
            await ac.aclose()

    async def test_reads_require_assets_read(self, app, db_session):
        from tests.test_ui_overhaul import _create_user, _login_as

        asset, _voice = await _seed_voice_asset(db_session)
        await _create_user(db_session, username="voice_viewer2", role_name="Viewer")
        ac = await _login_as(app, "voice_viewer2")
        try:
            resp = await ac.get(f"/api/voice-announcements/{asset.id}")
            assert resp.status_code == 200, resp.text
        finally:
            await ac.aclose()

    async def test_preview_returns_503_when_speech_unavailable(self, client):
        with patch(
            "cms.routers.voice_announcements.SpeechClient",
            side_effect=RuntimeError("constructor should not be called"),
        ), patch(
            "cms.routers.voice_announcements.is_available",
            return_value=False,
        ):
            resp = await client.get("/api/voice-announcements/voices?language=en-US")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["voices"] == []
        assert body["available"] is False
        assert "unavailable" in body["message"].lower()

    async def test_create_returns_503_when_speech_unavailable(self, client):
        with patch("cms.routers.voice_announcements.is_available", return_value=False):
            resp = await client.post("/api/voice-announcements", json=_payload())
        assert resp.status_code == 503

    async def test_update_requires_owner_or_admin(self, app, db_session):
        from tests.test_ui_overhaul import _create_user, _login_as

        owner = await _create_user(db_session, username="voice_owner", role_name="Operator")
        await _create_user(db_session, username="voice_other", role_name="Operator")
        asset, _voice = await _seed_voice_asset(db_session, owner_id=owner.id)
        ac = await _login_as(app, "voice_other")
        try:
            with patch("cms.routers.voice_announcements.is_available", return_value=True):
                resp = await ac.put(
                    f"/api/voice-announcements/{asset.id}",
                    json=_payload(script_text="Nope"),
                )
            assert resp.status_code == 403, resp.text
        finally:
            await ac.aclose()

    async def test_preview_returns_422_when_script_is_too_long(self, client):
        resp = await client.post(
            "/api/voice-announcements/preview",
            json=_payload(script_text="x" * 2001),
        )
        assert resp.status_code == 422

    async def test_voice_catalog_returns_live_voices(self, client):
        with patch("cms.routers.voice_announcements.is_available", return_value=True), patch(
            "cms.routers.voice_announcements.SpeechClient", _VoiceListSpeechClient
        ):
            resp = await client.get("/api/voice-announcements/voices?language=en-US")

        assert resp.status_code == 200, resp.text
        assert resp.json() == {
            "voices": [
                {
                    "short_name": "en-US-Ava:MAI-Voice-2",
                    "display_name": "Ava",
                    "locale": "en-US",
                    "emotions": ["cheerful", "sad"],
                }
            ],
            "available": True,
            "message": None,
        }

    async def test_voice_catalog_defaults_to_every_locale(self, client):
        """No ``language`` must mean no scoping, not an implicit en-US.

        The builder fetches once and builds its Language dropdown from the
        locales it gets back, so an implicit en-US default would silently
        hide every other language Microsoft offers.
        """
        _MultiLocaleSpeechClient.seen_language = "<unset>"
        with patch("cms.routers.voice_announcements.is_available", return_value=True), patch(
            "cms.routers.voice_announcements.SpeechClient", _MultiLocaleSpeechClient
        ):
            resp = await client.get("/api/voice-announcements/voices")

        assert resp.status_code == 200, resp.text
        assert _MultiLocaleSpeechClient.seen_language is None
        locales = sorted({v["locale"] for v in resp.json()["voices"]})
        assert locales == ["en-US", "fr-FR", "hu-HU"]

    async def test_voice_catalog_still_scopes_when_language_given(self, client):
        """An explicit ``language`` must keep filtering by locale prefix."""
        with patch("cms.routers.voice_announcements.is_available", return_value=True), patch(
            "cms.routers.voice_announcements.SpeechClient", _MultiLocaleSpeechClient
        ):
            resp = await client.get("/api/voice-announcements/voices?language=fr-FR")

        assert resp.status_code == 200, resp.text
        assert _MultiLocaleSpeechClient.seen_language == "fr-FR"
        assert [v["locale"] for v in resp.json()["voices"]] == ["fr-FR"]
