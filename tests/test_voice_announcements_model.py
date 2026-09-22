"""ORM tests for the VoiceAnnouncement model."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from cms.models.asset import Asset, AssetType
from cms.models.voice_announcement import VoiceAnnouncement
from shared.models.job import JobStatus


@pytest.mark.asyncio
async def test_voice_announcement_can_be_persisted_and_queried(db_session):
    asset = Asset(
        filename="announcement-1.ogg",
        asset_type=AssetType.VOICE_ANNOUNCEMENT,
        size_bytes=0,
        checksum="",
    )
    db_session.add(asset)
    await db_session.commit()

    announcement = VoiceAnnouncement(
        asset_id=asset.id,
        script_text="Attention shoppers, blue light special in aisle 5.",
        voice_name="en-US-Ava:MAI-Voice-2",
        emotion="cheerful",
        language="en-US",
        speech_rate="+10%",
        generation_status=JobStatus.PENDING,
    )
    db_session.add(announcement)
    await db_session.commit()

    res = await db_session.execute(
        select(VoiceAnnouncement).where(VoiceAnnouncement.asset_id == asset.id)
    )
    fetched = res.scalar_one()
    assert fetched.asset_id == asset.id
    assert fetched.script_text.startswith("Attention shoppers")
    assert fetched.voice_name == "en-US-Ava:MAI-Voice-2"
    assert fetched.generation_status == JobStatus.PENDING
    assert fetched.last_generated_at is None


@pytest.mark.asyncio
async def test_voice_announcement_cascade_deletes_with_asset(db_session):
    asset = Asset(
        filename="announcement-2.ogg",
        asset_type=AssetType.VOICE_ANNOUNCEMENT,
        size_bytes=0,
        checksum="",
    )
    db_session.add(asset)
    await db_session.commit()

    announcement = VoiceAnnouncement(
        asset_id=asset.id,
        script_text="Cleanup on aisle 3.",
        voice_name="en-US-Ava:MAI-Voice-2",
        language="en-US",
    )
    db_session.add(announcement)
    await db_session.commit()
    announcement_id = announcement.id

    await db_session.delete(asset)
    await db_session.commit()

    res = await db_session.execute(
        select(VoiceAnnouncement).where(VoiceAnnouncement.id == announcement_id)
    )
    assert res.scalar_one_or_none() is None


def test_asset_type_includes_voice_announcement():
    assert AssetType.VOICE_ANNOUNCEMENT.value == "voice_announcement"
