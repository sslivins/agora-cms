"""Voice-announcement API routes and ephemeral preview synthesis."""

from __future__ import annotations

import logging
import uuid
from datetime import timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cms.auth import get_settings, get_user_group_ids, require_auth, require_permission
from cms.config import Settings
from cms.database import get_db
from cms.models.asset import Asset, AssetType
from cms.models.user import User
from cms.models.voice_announcement import VoiceAnnouncement
from cms.permissions import ASSETS_READ, ASSETS_WRITE
from cms.schemas.voice_announcement import (
    VoiceAnnouncementCreate,
    VoiceAnnouncementCreateOut,
    VoiceAnnouncementPreviewIn,
    VoiceAnnouncementStatusOut,
    VoiceAnnouncementUpdate,
    VoiceCatalogOut,
)
from cms.services.audit_service import audit_log
from cms.services.speech_client import SpeechClient, SpeechUnavailableError, is_available
from shared.models.job import JobStatus, JobType
from shared.services.jobs import enqueue_job

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/voice-announcements",
    dependencies=[Depends(require_auth)],
    tags=["voice-announcements"],
)

_AUDIO_MEDIA_TYPE = "audio/ogg; codecs=opus"


async def _ensure_speech_available(settings: Settings) -> None:
    if not is_available(settings):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Voice synthesis is unavailable in this environment.",
        )


async def _load_voice_announcement(
    asset_id: uuid.UUID,
    request: Request,
    db: AsyncSession,
) -> tuple[Asset, VoiceAnnouncement]:
    from cms.routers.assets import _verify_asset_access  # noqa: WPS433

    await _verify_asset_access(asset_id, request, db)
    row = (
        await db.execute(
            select(Asset, VoiceAnnouncement)
            .join(VoiceAnnouncement, VoiceAnnouncement.asset_id == Asset.id)
            .where(Asset.id == asset_id, Asset.deleted_at.is_(None))
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Voice announcement not found")

    asset, voice_announcement = row
    if asset.asset_type != AssetType.VOICE_ANNOUNCEMENT:
        raise HTTPException(status_code=404, detail="Voice announcement not found")
    return asset, voice_announcement


async def _load_voice_announcement_for_write(
    asset_id: uuid.UUID,
    request: Request,
    user: User,
    db: AsyncSession,
) -> tuple[Asset, VoiceAnnouncement]:
    asset, voice_announcement = await _load_voice_announcement(asset_id, request, db)
    user_groups = await get_user_group_ids(user, db)
    is_admin = user_groups is None
    if not is_admin and asset.uploaded_by_user_id != user.id:
        raise HTTPException(
            status_code=403,
            detail="Only the voice announcement owner can edit it",
        )
    return asset, voice_announcement


def _status_out(voice_announcement: VoiceAnnouncement) -> VoiceAnnouncementStatusOut:
    last_generated_at = voice_announcement.last_generated_at
    if last_generated_at is not None and last_generated_at.tzinfo is None:
        last_generated_at = last_generated_at.replace(tzinfo=timezone.utc)
    return VoiceAnnouncementStatusOut(
        asset_id=voice_announcement.asset_id,
        generation_status=voice_announcement.generation_status,
        generation_error=voice_announcement.generation_error,
        last_generated_at=last_generated_at,
    )


@router.get("/voices", response_model=VoiceCatalogOut)
async def list_voice_catalog(
    language: str | None = Query(default="en-US"),
    settings: Settings = Depends(get_settings),
    _user: User = Depends(require_permission(ASSETS_READ)),
) -> VoiceCatalogOut:
    if not is_available(settings):
        return VoiceCatalogOut(
            voices=[],
            available=False,
            message="Voice synthesis is unavailable in this environment.",
        )

    try:
        async with SpeechClient(settings) as client:
            voices = await client.list_voices(language=language)
    except Exception:
        logger.warning(
            "voice_announcement.voice_catalog_fetch_failed language=%s",
            language or "",
            exc_info=True,
        )
        return VoiceCatalogOut(
            voices=[],
            available=False,
            message="Voice catalog is temporarily unavailable. Try again in a moment.",
        )

    return VoiceCatalogOut(voices=voices, available=True, message=None)


@router.post("", response_model=VoiceAnnouncementCreateOut, status_code=201)
async def create_voice_announcement(
    payload: VoiceAnnouncementCreate,
    request: Request,
    settings: Settings = Depends(get_settings),
    user: User = Depends(require_permission(ASSETS_WRITE)),
    db: AsyncSession = Depends(get_db),
) -> VoiceAnnouncementCreateOut:
    await _ensure_speech_available(settings)

    user_groups = await get_user_group_ids(user, db)
    make_global = user_groups is None
    asset_id = uuid.uuid4()
    filename = f"{asset_id}.ogg"

    asset = Asset(
        id=asset_id,
        filename=filename,
        display_name=payload.display_name,
        asset_type=AssetType.VOICE_ANNOUNCEMENT,
        size_bytes=0,
        checksum="",
        duration_seconds=None,
        audio_codec=None,
        is_global=make_global,
        uploaded_by_user_id=user.id,
    )
    db.add(asset)

    voice_announcement = VoiceAnnouncement(
        asset_id=asset_id,
        script_text=payload.script_text,
        voice_name=payload.voice_name,
        emotion=payload.emotion,
        language=payload.language,
        speech_rate=payload.speech_rate,
        generation_status=JobStatus.PENDING,
        generation_error=None,
        last_generated_at=None,
    )
    db.add(voice_announcement)
    await db.flush()

    await audit_log(
        db,
        user=user,
        action="asset.create_voice_announcement",
        resource_type="asset",
        resource_id=str(asset_id),
        description=f"Created voice announcement '{payload.display_name}'",
        details={
            "display_name": payload.display_name,
            "filename": filename,
            "language": payload.language,
            "voice_name": payload.voice_name,
            "emotion": payload.emotion,
            "speech_rate": payload.speech_rate,
            "script_length": len(payload.script_text),
            "is_global": make_global,
        },
        request=request,
    )
    await enqueue_job(db, JobType.VOICE_SYNTHESIS, asset_id)
    return VoiceAnnouncementCreateOut(
        asset_id=asset_id,
        generation_status=JobStatus.PENDING,
        edit_url=f"/assets/{asset_id}/voice",
    )


@router.get("/{asset_id}", response_model=VoiceAnnouncementStatusOut)
async def get_voice_announcement_status(
    asset_id: uuid.UUID,
    request: Request,
    _user: User = Depends(require_permission(ASSETS_READ)),
    db: AsyncSession = Depends(get_db),
) -> VoiceAnnouncementStatusOut:
    _asset, voice_announcement = await _load_voice_announcement(asset_id, request, db)
    return _status_out(voice_announcement)


@router.post("/preview")
async def preview_voice_announcement(
    payload: VoiceAnnouncementPreviewIn,
    settings: Settings = Depends(get_settings),
    _user: User = Depends(require_permission(ASSETS_WRITE)),
) -> Response:
    try:
        async with SpeechClient(settings) as client:
            audio_bytes = await client.synthesize(
                payload.script_text,
                voice_name=payload.voice_name,
                emotion=payload.emotion,
                language=payload.language,
                speech_rate=payload.speech_rate,
            )
    except SpeechUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc

    return Response(content=audio_bytes, media_type=_AUDIO_MEDIA_TYPE)


@router.put("/{asset_id}", response_model=VoiceAnnouncementStatusOut)
async def update_voice_announcement(
    asset_id: uuid.UUID,
    payload: VoiceAnnouncementUpdate,
    request: Request,
    settings: Settings = Depends(get_settings),
    user: User = Depends(require_permission(ASSETS_WRITE)),
    db: AsyncSession = Depends(get_db),
) -> VoiceAnnouncementStatusOut:
    await _ensure_speech_available(settings)
    asset, voice_announcement = await _load_voice_announcement_for_write(
        asset_id, request, user, db
    )

    if payload.display_name is not None:
        asset.display_name = payload.display_name
    voice_announcement.script_text = payload.script_text
    voice_announcement.voice_name = payload.voice_name
    voice_announcement.emotion = payload.emotion
    voice_announcement.language = payload.language
    voice_announcement.speech_rate = payload.speech_rate
    voice_announcement.generation_status = JobStatus.PENDING
    voice_announcement.generation_error = None
    voice_announcement.last_generated_at = None

    asset.size_bytes = 0
    asset.checksum = ""
    asset.duration_seconds = None
    asset.audio_codec = None

    await audit_log(
        db,
        user=user,
        action="asset.update_voice_announcement",
        resource_type="asset",
        resource_id=str(asset_id),
        description=(
            "Updated voice announcement "
            f"'{asset.display_name or asset.original_filename or asset.filename}'"
        ),
        details={
            "display_name": asset.display_name,
            "language": payload.language,
            "voice_name": payload.voice_name,
            "emotion": payload.emotion,
            "speech_rate": payload.speech_rate,
            "script_length": len(payload.script_text),
            "generation_status": JobStatus.PENDING.value,
        },
        request=request,
    )
    await enqueue_job(db, JobType.VOICE_SYNTHESIS, asset_id)
    return _status_out(voice_announcement)
