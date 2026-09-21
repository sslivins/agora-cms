"""Worker handler for Voice Announcement synthesis jobs."""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from cms.models.voice_announcement import VoiceAnnouncement
from cms.services.speech_client import SpeechClient
from shared.models.asset import Asset, AssetType
from shared.models.job import JobStatus
from shared.services.probe import probe_media
from shared.services.storage import get_storage
from worker.config import WorkerSettings

logger = logging.getLogger("agora.worker.voice_synthesis")


async def synthesize_voice_announcement_by_id(
    session_factory, asset_dir: Path, asset_id: uuid.UUID
) -> bool:
    """Synthesize one specific VOICE_ANNOUNCEMENT asset by UUID."""
    output_path: Path | None = None
    async with session_factory() as db:
        result = await db.execute(
            select(Asset, VoiceAnnouncement)
            .join(VoiceAnnouncement, VoiceAnnouncement.asset_id == Asset.id)
            .where(Asset.id == asset_id)
        )
        row = result.one_or_none()
        if row is None:
            logger.info(
                "Voice announcement asset %s no longer exists — skipping", asset_id
            )
            return False

        asset, voice_announcement = row
        if asset.asset_type != AssetType.VOICE_ANNOUNCEMENT:
            logger.info(
                "Asset %s is not a VOICE_ANNOUNCEMENT — skipping synthesis", asset_id
            )
            return False
        if (
            voice_announcement.generation_status == JobStatus.DONE
            and asset.size_bytes > 0
        ):
            logger.info("Voice announcement %s already generated — skipping", asset_id)
            return True

        voice_announcement.generation_status = JobStatus.PROCESSING
        voice_announcement.generation_error = None
        await db.commit()

        try:
            settings = WorkerSettings()
            async with SpeechClient(settings) as client:
                audio_bytes = await client.synthesize(
                    voice_announcement.script_text,
                    voice_name=voice_announcement.voice_name,
                    emotion=voice_announcement.emotion,
                    language=voice_announcement.language,
                    speech_rate=voice_announcement.speech_rate,
                )

            output_path = asset_dir / asset.filename
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(audio_bytes)

            meta = await probe_media(output_path)
            file_size = output_path.stat().st_size
            sha = hashlib.sha256()
            with open(output_path, "rb") as f:
                while chunk := f.read(1024 * 1024):
                    sha.update(chunk)

            asset.size_bytes = file_size
            asset.checksum = sha.hexdigest()
            asset.duration_seconds = meta.get("duration_seconds")
            asset.audio_codec = "opus"

            voice_announcement.generation_status = JobStatus.DONE
            voice_announcement.generation_error = None
            voice_announcement.last_generated_at = datetime.now(timezone.utc)
            await db.commit()

            storage = get_storage()
            await storage.on_file_stored(asset.filename)

            logger.info(
                "Voice announcement synthesis complete: %s (%d bytes)",
                asset.filename,
                file_size,
            )
            return True
        except Exception as exc:
            logger.exception(
                "Voice announcement synthesis failed for asset %s", asset_id
            )
            voice_announcement.generation_status = JobStatus.FAILED
            voice_announcement.generation_error = str(exc)[:2000]
            asset.size_bytes = 0
            asset.checksum = ""
            asset.duration_seconds = None
            asset.audio_codec = None
            await db.commit()
            if output_path is not None:
                try:
                    output_path.unlink(missing_ok=True)
                except OSError:
                    logger.debug("voice output cleanup failed", exc_info=True)
            return False
