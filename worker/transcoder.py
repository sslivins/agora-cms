"""FFmpeg transcoding engine — runs in the dedicated worker container.

Contains all video/image transcoding logic, moved from cms/services/transcoder.py.
Imports models and services from the shared package.
"""

import asyncio
import hashlib
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import SQLAlchemyError

from shared.models.asset import Asset, AssetType, AssetVariant, VariantStatus
from shared.models.device_profile import DeviceProfile
from shared.services.image import convert_image, image_variant_ext
from shared.services.probe import probe_media
from shared.services.storage import get_storage

logger = logging.getLogger("agora.worker.transcoder")

# Maximum duration to capture from a stream (seconds).  Prevents runaway
# captures of truly-live streams that never end.
STREAM_CAPTURE_MAX_SECONDS = int(os.environ.get("AGORA_STREAM_CAPTURE_MAX_SECONDS", "14400"))  # 4 hours

# Max retries for SAVED_STREAM failures (network issues, timing, etc.)
STREAM_MAX_RETRIES = int(os.environ.get("AGORA_STREAM_MAX_RETRIES", "3"))

# Errors that should NOT be retried (bad input, not transient)
_NO_RETRY_ERRORS = {"Image conversion failed", "Invalid data found"}


def _should_retry(variant: AssetVariant, source: Asset) -> bool:
    """Return True if this variant failure is retryable.

    Job-level retry is handled by the queue (see ``shared.services.jobs``).
    This hook only controls whether the variant row is flipped back to
    PENDING so a retry is actually meaningful.  Non-stream assets are
    never retried — transcode is deterministic.
    """
    if source.asset_type != AssetType.SAVED_STREAM:
        return False
    msg = variant.error_message or ""
    return not any(pat in msg for pat in _NO_RETRY_ERRORS)


# ── Active transcode tracking (cancel support) ─────────────────

_active_process = None  # asyncio.subprocess.Process | None
_active_profile_id: uuid.UUID | None = None
_active_source_asset_id: uuid.UUID | None = None
_active_variant_id: uuid.UUID | None = None
_cancelled_variant_ids: set[uuid.UUID] = set()


def cancel_profile_transcodes(profile_id: uuid.UUID) -> bool:
    """Kill the active ffmpeg process if it belongs to the given profile."""
    global _active_process, _active_profile_id, _active_variant_id
    if (
        _active_process is not None
        and _active_profile_id == profile_id
        and _active_process.returncode is None
    ):
        logger.info(
            "Cancelling active transcode for profile %s (variant %s)",
            profile_id, _active_variant_id,
        )
        _cancelled_variant_ids.add(_active_variant_id)
        _active_process.terminate()
        return True
    return False


def cancel_asset_transcodes(asset_id: uuid.UUID) -> bool:
    """Kill the active ffmpeg process if it belongs to the given source asset."""
    global _active_process, _active_source_asset_id, _active_variant_id
    if (
        _active_process is not None
        and _active_source_asset_id == asset_id
        and _active_process.returncode is None
    ):
        logger.info(
            "Cancelling active transcode for asset %s (variant %s)",
            asset_id, _active_variant_id,
        )
        _cancelled_variant_ids.add(_active_variant_id)
        _active_process.terminate()
        return True
    return False


def cancel_active_ffmpeg() -> bool:
    """Unconditionally SIGTERM the active ffmpeg subprocess, if any.

    Used by the queue worker's heartbeat when it observes
    ``Job.cancel_requested``.  Adds the active variant id to
    ``_cancelled_variant_ids`` so ``_transcode_one`` treats the non-zero
    exit as a clean cancellation instead of a failure.
    """
    global _active_process, _active_variant_id
    if (
        _active_process is not None
        and _active_process.returncode is None
    ):
        logger.info("Cancelling active ffmpeg (variant %s)", _active_variant_id)
        if _active_variant_id is not None:
            _cancelled_variant_ids.add(_active_variant_id)
        try:
            _active_process.terminate()
        except Exception:
            logger.warning("Failed to terminate ffmpeg process", exc_info=True)
        return True
    return False


async def _source_asset_is_deleted(db: AsyncSession, asset_id: uuid.UUID) -> bool:
    """Re-read ``assets.deleted_at`` fresh from DB (no session cache).

    Called as a final-status guard before marking variants READY: if the
    user soft-deleted the asset while ffmpeg was running, we skip the
    READY write so the reaper can clean up cleanly without having to
    chase a race-condition zombie.
    """
    result = await db.execute(
        select(Asset.deleted_at).where(Asset.id == asset_id)
    )
    deleted_at = result.scalar_one_or_none()
    return deleted_at is not None


async def _abort_on_deleted(variant, output_path, db: AsyncSession) -> None:
    """Cleanup partial output + leave variant un-READY when asset is gone.

    Idempotent.  The CMS reaper will hard-delete the variant row shortly.
    """
    try:
        if output_path.is_file():
            output_path.unlink()
    except Exception:
        logger.warning("Failed to unlink %s on deleted-asset abort", output_path, exc_info=True)
    # Leave variant status alone — reaper will drop the row once all jobs
    # are terminal.  No commit needed.
    logger.info(
        "Variant %s: source asset soft-deleted — skipping READY write",
        variant.id,
    )


# ── FFmpeg codec / color space maps ─────────────────────────────

CODEC_ENCODER_MAP: dict[str, str] = {
    "h264": "libx264",
    "h265": "libx265",
    "av1": "libsvtav1",
}

COLOR_SPACE_MAP: dict[str, dict[str, str]] = {
    "bt709":      {"colorspace": "bt709",    "color_primaries": "bt709",  "color_trc": "bt709"},
    "smpte170m":  {"colorspace": "smpte170m","color_primaries": "smpte170m","color_trc": "smpte170m"},
    "bt2020-pq":  {"colorspace": "bt2020nc", "color_primaries": "bt2020", "color_trc": "smpte2084"},
    "bt2020-hlg": {"colorspace": "bt2020nc", "color_primaries": "bt2020", "color_trc": "arib-std-b67"},
}

_HDR_TRCS = {"smpte2084", "arib-std-b67"}
_HDR_TARGET_CS = {"bt2020-pq", "bt2020-hlg"}


# ── FFmpeg argument builder ─────────────────────────────────────

def _build_ffmpeg_args_safe(
    source_path: Path,
    output_path: Path,
    profile: DeviceProfile,
    *,
    source_color_trc: str | None = None,
) -> list[str]:
    """Build ffmpeg command-line arguments for a given profile."""
    max_w = profile.max_width
    max_h = profile.max_height

    pix_fmt = profile.pixel_format or "auto"
    cs_key = profile.color_space or "auto"
    cs = COLOR_SPACE_MAP.get(cs_key) if cs_key != "auto" else None

    needs_tonemap = (
        source_color_trc in _HDR_TRCS
        and cs_key not in _HDR_TARGET_CS
    )

    if pix_fmt == "auto":
        _420_only_profiles = {
            "h264": {"baseline", "main", "high", "high10"},
            "h265": {"main", "main10"},
        }
        restricted = _420_only_profiles.get(profile.video_codec, set())
        if needs_tonemap or profile.video_codec == "av1" or profile.video_profile in restricted:
            pix_fmt = "yuv420p"

    if needs_tonemap:
        cs = COLOR_SPACE_MAP["bt709"]

    scale_parts = [
        f"scale=w='if(gt(iw,{max_w}),{max_w},iw)':h='if(gt(ih,{max_h}),{max_h},ih)'"
        f":force_original_aspect_ratio=decrease",
        "pad=ceil(iw/2)*2:ceil(ih/2)*2",
    ]

    if needs_tonemap:
        scale_parts.extend([
            "zscale=t=linear:npl=100",
            "format=gbrpf32le",
            "tonemap=hable",
            "zscale=p=bt709:t=bt709:m=bt709:r=tv",
        ])

    if pix_fmt != "auto":
        scale_parts.append(f"format={pix_fmt}")
    if cs:
        scale_parts.append(
            f"setparams=colorspace={cs['colorspace']}"
            f":color_primaries={cs['color_primaries']}"
            f":color_trc={cs['color_trc']}"
        )
    scale_filter = ",".join(scale_parts)

    encoder = CODEC_ENCODER_MAP.get(profile.video_codec, "libx264")

    args = [
        "ffmpeg", "-y",
        # Tolerate corrupt frames (e.g. bad NAL units from HLS captures)
        "-err_detect", "ignore_err",
        "-i", str(source_path),
        "-c:v", encoder,
    ]

    if profile.video_profile and profile.video_codec in ("h264", "h265"):
        args.extend(["-profile:v", profile.video_profile])

    args.extend(["-vf", scale_filter])
    args.extend(["-r", str(profile.max_fps)])

    if cs:
        args.extend([
            "-colorspace", cs["colorspace"],
            "-color_primaries", cs["color_primaries"],
            "-color_trc", cs["color_trc"],
        ])

    if profile.video_bitrate:
        bitrate = profile.video_bitrate
        if bitrate and not bitrate[-1].isalpha():
            bitrate = f"{bitrate}M"
        args.extend(["-b:v", bitrate])
    else:
        args.extend(["-crf", str(profile.crf)])

    args.extend([
        "-c:a", profile.audio_codec,
        "-b:a", profile.audio_bitrate,
    ])

    # +faststart rewrites the entire file (moving moov atom to front).
    # On Azure Files SMB mounts, this seek-heavy second pass corrupts
    # data blocks.  Azure Blob Storage serves variants with HTTP range
    # requests, so progressive-download optimisation is unnecessary.
    # When running on local storage the rewrite would be safe, but we
    # skip it unconditionally for consistency.
    # ext = output_path.suffix.lower()
    # if ext != ".mkv":
    #     args.append("-movflags")
    #     args.append("+faststart")

    args.append(str(output_path))

    return args


# ── Duration helper ─────────────────────────────────────────────

async def _get_duration(source: Path | str) -> float | None:
    """Get duration of a media file in seconds using ffprobe.

    Accepts a local ``Path`` or a remote URL string.  Returns ``None`` for
    livestreams (ffprobe reports ``N/A``) or any probe failure.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(source),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        return float(stdout.decode().strip())
    except (ValueError, OSError):
        return None


# ── Stream capture ──────────────────────────────────────────────

async def _capture_stream(asset: Asset, asset_dir: Path, db: AsyncSession) -> Path | None:
    """Download a stream URL to a local MP4 file using FFmpeg.

    Returns the path to the captured file, or None on failure.
    The captured file is stored as the asset's source file so subsequent
    per-profile transcoding can pick it up like any uploaded video.
    """
    global _active_process

    url = asset.url
    if not url:
        return None

    capture_filename = f"{asset.id}_capture.mp4"
    capture_path = asset_dir / capture_filename

    max_duration = asset.capture_duration or STREAM_CAPTURE_MAX_SECONDS

    # Probe the source upfront with the same helper used for uploaded files.
    # ffprobe returns a finite duration for VOD (HLS with ENDLIST, MP4, etc.)
    # and None for true livestreams — we use that to pick a progress denom.
    probed = await _get_duration(url)
    if probed and probed > 0:
        progress_denom = min(float(max_duration), probed)
        logger.info(
            "Capturing VOD stream %s → %s (duration %.1fs, max %ds)",
            url, capture_filename, probed, max_duration,
        )
    else:
        progress_denom = float(max_duration)
        logger.info(
            "Capturing live stream %s → %s (max %ds)",
            url, capture_filename, max_duration,
        )

    # Reset progress/error state before starting so retries don't show stale data
    asset.capture_progress = 0.0
    asset.capture_error = None
    try:
        await db.commit()
    except SQLAlchemyError:
        logger.warning("Failed to reset capture_progress for asset %s", asset.id)

    args = [
        "ffmpeg", "-y",
        # Network / stream input options
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "5",
        # ``-t`` is a safety ceiling.  For VOD, ffmpeg exits naturally at
        # end-of-stream (usually much sooner); for livestreams it caps the
        # capture at the configured duration.
        "-t", str(max_duration),
        "-i", url,
        # Copy codecs (no re-encode during capture — transcoding happens later).
        # Do NOT use +faststart here: this is an intermediate file read only by
        # the worker.  The second-pass rewrite corrupts data on Azure Files SMB
        # mounts due to the seek-heavy I/O pattern over the network filesystem.
        "-c", "copy",
        str(capture_path),
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _active_process = proc

        # Stream stderr so we can parse ffmpeg's periodic `time=HH:MM:SS.ss`
        # markers and surface live capture progress in the UI.  Same pattern
        # used by _transcode_one for variant progress.
        stderr_data = b""
        last_progress_update = 0.0
        while True:
            chunk = await proc.stderr.read(4096)
            if not chunk:
                break
            stderr_data += chunk
            if progress_denom:
                decoded = chunk.decode("utf-8", errors="replace")
                matches = list(re.finditer(r"time=(\d+):(\d+):(\d+\.\d+)", decoded))
                if matches:
                    match = matches[-1]
                    h, m, s = int(match.group(1)), int(match.group(2)), float(match.group(3))
                    elapsed = h * 3600 + m * 60 + s
                    pct = min(99.0, (elapsed / progress_denom) * 100)
                    if pct > last_progress_update and pct - last_progress_update >= 1.0:
                        # Cooperative cancel probe: if the user soft-deleted
                        # the asset mid-capture, terminate ffmpeg so the
                        # reaper can hard-delete the row promptly.
                        if await _source_asset_is_deleted(db, asset.id):
                            logger.info(
                                "Asset %s: soft-deleted mid-capture — cancelling ffmpeg",
                                asset.id,
                            )
                            try:
                                proc.terminate()
                            except Exception:
                                logger.warning("Failed to terminate ffmpeg", exc_info=True)
                            try:
                                await proc.wait()
                            except Exception:
                                pass
                            _active_process = None
                            try:
                                if capture_path.is_file():
                                    capture_path.unlink()
                            except Exception:
                                pass
                            return None
                        asset.capture_progress = round(pct, 1)
                        try:
                            await db.commit()
                        except SQLAlchemyError:
                            logger.warning(
                                "Capture progress commit failed for asset %s — "
                                "asset may have been deleted; continuing capture",
                                asset.id,
                            )
                        last_progress_update = pct

        await proc.wait()
        _active_process = None

        if proc.returncode != 0:
            full_text = stderr_data.decode("utf-8", errors="replace")
            error_text = full_text[:500] if len(full_text) <= 500 else full_text[:250] + "\n…\n" + full_text[-250:]
            logger.error("Stream capture failed for %s: exit %d\n%s", url, proc.returncode, error_text)
            asset.capture_error = f"ffmpeg exit code {proc.returncode}: {error_text}"
            try:
                await db.commit()
            except SQLAlchemyError:
                logger.warning("Failed to persist capture_error for asset %s", asset.id)
            return None

        if not capture_path.is_file() or capture_path.stat().st_size == 0:
            logger.error("Stream capture produced empty file for %s", url)
            asset.capture_error = "Stream capture produced empty file"
            try:
                await db.commit()
            except SQLAlchemyError:
                pass
            return None

        # Probe the captured file for metadata
        meta = await probe_media(capture_path)
        file_size = capture_path.stat().st_size
        sha = hashlib.sha256()
        with open(capture_path, "rb") as f:
            while chunk := f.read(1024 * 1024):
                sha.update(chunk)

        # Formalize: store display name in original_filename, set filename
        # to the actual capture file so delete/retranscode/storage all work.
        if asset.filename != capture_filename:
            asset.original_filename = asset.filename
            asset.filename = capture_filename

        # Update the source asset with captured file metadata
        asset.size_bytes = file_size
        asset.checksum = sha.hexdigest()
        asset.capture_progress = 100.0
        asset.capture_error = None
        for key, val in meta.items():
            if val is not None and hasattr(asset, key):
                setattr(asset, key, val)

        await db.commit()

        # Sync captured file to cloud storage
        storage = get_storage()
        await storage.on_file_stored(capture_filename)

        logger.info(
            "Stream capture complete: %s (%d bytes, %.1fs)",
            capture_filename, file_size, meta.get("duration_seconds") or 0,
        )
        return capture_path

    except Exception as e:
        _active_process = None
        logger.exception("Stream capture error for %s: %s", url, e)
        try:
            asset.capture_error = f"Capture exception: {e}"
            await db.commit()
        except SQLAlchemyError:
            pass
        return None


# ── Single variant transcoding ──────────────────────────────────

async def _mark_failed(variant: AssetVariant, source: Asset, message: str, db: AsyncSession) -> None:
    """Mark a variant as FAILED or re-queue for retry (SAVED_STREAM only).

    Job-level retry is handled by the queue/jobs system.  For SAVED_STREAM
    transients we leave the variant PENDING so the next VARIANT_TRANSCODE
    job (from the monitor loop or a fresh queue delivery) will pick it up.
    """
    variant.error_message = message[:500]
    if _should_retry(variant, source):
        variant.status = VariantStatus.PENDING
        variant.progress = 0.0
        logger.warning("Variant %s retry pending: %s", variant.id, message)
    else:
        variant.status = VariantStatus.FAILED
        variant.progress = 0.0
        logger.error("Variant %s failed permanently: %s", variant.id, message)
    try:
        await db.commit()
    except SQLAlchemyError:
        logger.warning("Variant %s deleted before failure recorded", variant.id)

async def _transcode_one(variant: AssetVariant, db: AsyncSession, asset_dir: Path) -> None:
    """Transcode a single variant using ffmpeg."""
    await db.refresh(variant, ["source_asset", "profile"])
    source = variant.source_asset
    profile = variant.profile

    # Resolve source file path.  For uploads and captured streams, prefer
    # the original file when present (better quality than any variant).
    if source.original_filename:
        original_path = asset_dir / "originals" / source.original_filename
        if original_path.is_file():
            source_path = original_path
        else:
            source_path = asset_dir / source.filename
    else:
        source_path = asset_dir / source.filename

    variants_dir = asset_dir / "variants"
    variants_dir.mkdir(parents=True, exist_ok=True)

    # For image assets, force the correct output extension
    if source.asset_type == AssetType.IMAGE:
        correct_ext = image_variant_ext(source.filename)
        output_path = variants_dir / (variant.filename.rsplit(".", 1)[0] + correct_ext)
    else:
        output_path = variants_dir / variant.filename

    if not source_path.is_file():
        await _mark_failed(variant, source, "Source file not found", db)
        return

    # Mark as processing
    variant.status = VariantStatus.PROCESSING
    variant.progress = 0.0
    await db.commit()

    # Track active transcode for cancellation
    global _active_process, _active_profile_id, _active_source_asset_id, _active_variant_id
    _active_profile_id = variant.profile_id
    _active_source_asset_id = variant.source_asset_id
    _active_variant_id = variant.id

    logger.info(
        "Transcoding %s → %s (profile: %s)",
        source.filename, variant.filename, profile.name,
    )

    # Image assets: convert/downscale at profile max dimensions
    if source.asset_type == AssetType.IMAGE:
        try:
            ok = await convert_image(
                source_path, output_path,
                max_width=profile.max_width,
                max_height=profile.max_height,
            )
            if not ok:
                await _mark_failed(variant, source, "Image conversion failed", db)
                return

            file_size = output_path.stat().st_size
            sha = hashlib.sha256()
            with open(output_path, "rb") as f:
                while chunk := f.read(1024 * 1024):
                    sha.update(chunk)

            # Final-status guard: if the user soft-deleted the asset mid-
            # conversion, skip the READY write and let the reaper clean up.
            if await _source_asset_is_deleted(db, variant.source_asset_id):
                await _abort_on_deleted(variant, output_path, db)
                return

            variant.checksum = sha.hexdigest()
            variant.size_bytes = file_size
            variant.status = VariantStatus.READY
            variant.progress = 100.0
            variant.completed_at = datetime.now(timezone.utc)

            meta = await probe_media(output_path)
            for key, val in meta.items():
                if val is not None:
                    setattr(variant, key, val)

            try:
                await db.commit()
            except SQLAlchemyError:
                logger.warning("Variant %s deleted before image completion recorded", variant.id)
                return
            logger.info("Image variant complete: %s (%d bytes)", variant.filename, variant.size_bytes)

            # Sync variant to cloud storage
            storage = get_storage()
            await storage.on_file_stored(f"variants/{variant.filename}")

            return
        except Exception as e:
            await _mark_failed(variant, source, str(e)[:500], db)
            logger.exception("Image variant error for %s", variant.filename)
            return

    # Video assets: full ffmpeg transcode
    duration = await _get_duration(source_path)
    source_meta = await probe_media(source_path)

    args = _build_ffmpeg_args_safe(
        source_path, output_path, profile,
        source_color_trc=source_meta.get("color_transfer"),
    )

    try:
        env = os.environ.copy()
        proc = await asyncio.create_subprocess_exec(
            "nice", "-n", "15", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        _active_process = proc

        stderr_data = b""
        last_progress_update = 0.0
        while True:
            chunk = await proc.stderr.read(4096)
            if not chunk:
                break
            stderr_data += chunk

            if duration:
                decoded = chunk.decode("utf-8", errors="replace")
                matches = list(re.finditer(r"time=(\d+):(\d+):(\d+\.\d+)", decoded))
                if matches:
                    match = matches[-1]
                    h, m, s = int(match.group(1)), int(match.group(2)), float(match.group(3))
                    elapsed = h * 3600 + m * 60 + s
                    pct = min(99.0, (elapsed / duration) * 100)
                    # Only update if progress moved forward (monotonic)
                    if pct > last_progress_update and pct - last_progress_update >= 1.0:
                        # Cooperative cancel probe: if the user soft-deleted
                        # the source asset mid-transcode, terminate ffmpeg so
                        # the reaper can hard-delete promptly.  In Azure
                        # queue-mode the 15s heartbeat handles this; LISTEN/
                        # NOTIFY mode has no heartbeat, so we do it here.
                        if await _source_asset_is_deleted(db, variant.source_asset_id):
                            logger.info(
                                "Variant %s: source asset soft-deleted mid-transcode — cancelling ffmpeg",
                                variant.id,
                            )
                            _cancelled_variant_ids.add(variant.id)
                            try:
                                proc.terminate()
                            except Exception:
                                logger.warning("Failed to terminate ffmpeg", exc_info=True)
                            try:
                                await proc.wait()
                            except Exception:
                                pass
                            _active_process = None
                            await _abort_on_deleted(variant, output_path, db)
                            return
                        variant.progress = round(pct, 1)
                        try:
                            await db.commit()
                        except SQLAlchemyError:
                            # Variant was deleted while transcoding — abort
                            logger.warning("Variant %s deleted during transcode, aborting", variant.id)
                            proc.kill()
                            return
                        last_progress_update = pct

        await proc.wait()
        _active_process = None

        if proc.returncode != 0:
            if variant.id in _cancelled_variant_ids:
                _cancelled_variant_ids.discard(variant.id)
                logger.info("Transcode cancelled for %s (profile updated)", variant.filename)
                return
            full_text = stderr_data.decode("utf-8", errors="replace")
            error_text = full_text[:500] if len(full_text) <= 500 else full_text[:250] + "\n…\n" + full_text[-250:]
            msg = f"ffmpeg exit code {proc.returncode}: {error_text}"
            await _mark_failed(variant, source, msg, db)
            return

        # Success
        file_size = output_path.stat().st_size
        sha = hashlib.sha256()
        with open(output_path, "rb") as f:
            while chunk := f.read(1024 * 1024):
                sha.update(chunk)

        # Final-status guard: if the user soft-deleted the asset mid-
        # transcode, skip the READY write and let the reaper clean up.
        if await _source_asset_is_deleted(db, variant.source_asset_id):
            await _abort_on_deleted(variant, output_path, db)
            return

        variant.checksum = sha.hexdigest()
        variant.size_bytes = file_size
        variant.status = VariantStatus.READY
        variant.progress = 100.0
        variant.completed_at = datetime.now(timezone.utc)

        meta = await probe_media(output_path)
        for key, val in meta.items():
            if val is not None:
                setattr(variant, key, val)

        try:
            await db.commit()
        except SQLAlchemyError:
            logger.warning("Variant %s deleted before completion could be recorded", variant.id)
            return
        logger.info("Transcode complete: %s (%d bytes)", variant.filename, variant.size_bytes)

        # Sync variant to cloud storage
        storage = get_storage()
        await storage.on_file_stored(f"variants/{variant.filename}")

    except Exception as e:
        _active_process = None
        await _mark_failed(variant, source, str(e)[:500], db)
        logger.exception("Transcode error for %s", variant.filename)


# ── Batch processing ────────────────────────────────────────────

async def recover_interrupted(session_factory) -> int:
    """Reset any variants left in PROCESSING state from a previous crash."""
    async with session_factory() as db:
        result = await db.execute(
            select(AssetVariant).where(
                AssetVariant.status == VariantStatus.PROCESSING
            )
        )
        stuck = result.scalars().all()
        if stuck:
            for v in stuck:
                v.status = VariantStatus.PENDING
                v.progress = 0
            await db.commit()
            logger.info(
                "Reset %d interrupted PROCESSING variant(s) to PENDING",
                len(stuck),
            )
        return len(stuck)


async def process_captures(session_factory, asset_dir: Path) -> int:
    """Find SAVED_STREAM assets that need capturing and capture them.

    A SAVED_STREAM needs capture when it has a URL, has no variants yet,
    and its source file does not exist on disk (i.e. hasn't been captured).
    Uses FOR UPDATE SKIP LOCKED so parallel workers don't grab the same
    stream.

    Returns the number of streams captured.
    """
    from sqlalchemy import func

    count = 0
    while True:
        asset_id = None

        # ── Claim one uncaptured SAVED_STREAM atomically ──
        async with session_factory() as db:
            # Subquery: assets that already have at least one variant
            has_variants = (
                select(AssetVariant.source_asset_id)
                .group_by(AssetVariant.source_asset_id)
                .having(func.count() > 0)
                .correlate(Asset)
            )
            result = await db.execute(
                select(Asset)
                .where(
                    Asset.asset_type == AssetType.SAVED_STREAM,
                    Asset.url.isnot(None),
                    Asset.size_bytes == 0,  # not yet captured (capture sets this)
                    Asset.id.notin_(has_variants),  # no variants created yet
                )
                .order_by(Asset.uploaded_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            asset = result.scalar_one_or_none()
            if asset is None:
                break
            asset_id = asset.id
            logger.info("Claiming stream capture for asset %s (%s)", asset.id, asset.url)

        # ── Capture in a fresh session (row lock released above) ──
        async with session_factory() as db:
            result = await db.execute(
                select(Asset).where(Asset.id == asset_id)
            )
            asset = result.scalar_one_or_none()
            if asset is None:
                continue

            capture_path = await _capture_stream(asset, asset_dir, db)
            if capture_path is None:
                logger.error("Stream capture failed for asset %s", asset.id)
                # Leave the asset as-is — the CMS monitor can retry or alert
            else:
                count += 1
                logger.info("Stream capture complete for asset %s", asset.id)

    return count


async def process_pending(session_factory, asset_dir: Path) -> int:
    """Process all pending variants. Returns number processed."""
    count = 0
    while True:
        # ── Claim one PENDING variant atomically ──
        # FOR UPDATE SKIP LOCKED prevents parallel KEDA-triggered workers
        # from grabbing the same row.  Setting PROCESSING inside the same
        # short transaction makes the claim visible immediately.
        variant_id = None
        async with session_factory() as db:
            result = await db.execute(
                select(AssetVariant)
                .join(Asset, AssetVariant.source_asset_id == Asset.id)
                .where(
                    AssetVariant.status == VariantStatus.PENDING,
                    Asset.deleted_at.is_(None),
                )
                .order_by(AssetVariant.created_at)
                .limit(1)
                .with_for_update(skip_locked=True, of=AssetVariant)
            )
            variant = result.scalar_one_or_none()
            if variant is None:
                break
            variant_id = variant.id
            variant.status = VariantStatus.PROCESSING
            variant.progress = 0.0
            await db.commit()

        # ── Process in a fresh session (row lock released above) ──
        async with session_factory() as db:
            result = await db.execute(
                select(AssetVariant).where(AssetVariant.id == variant_id)
            )
            variant = result.scalar_one_or_none()
            if variant is None:
                continue
            await _transcode_one(variant, db, asset_dir)
            count += 1

    return count


# ── Direct-dispatch helpers (used by queue-mode workers) ────────

async def transcode_variant_by_id(
    session_factory, asset_dir: Path, variant_id: uuid.UUID
) -> bool:
    """Transcode one specific variant identified by its UUID.

    Returns True on success, False if the variant row no longer exists.
    Raises on transcode failure so the caller can leave the queue message
    undeleted (triggering a retry after the visibility timeout).

    Queue is authority: we do NOT use SKIP LOCKED here — the caller already
    owns the lease via the queue message.
    """
    async with session_factory() as db:
        result = await db.execute(
            select(AssetVariant).where(AssetVariant.id == variant_id)
        )
        variant = result.scalar_one_or_none()
        if variant is None:
            logger.info("Variant %s no longer exists — skipping", variant_id)
            return False
        if variant.status == VariantStatus.READY:
            logger.info("Variant %s already READY — skipping", variant_id)
            return True
        await _transcode_one(variant, db, asset_dir)
        # _transcode_one sets the variant to READY or FAILED internally.
        # Refresh and report success iff the final status is READY.
        await db.refresh(variant)
        return variant.status == VariantStatus.READY


async def capture_stream_by_id(
    session_factory, asset_dir: Path, asset_id: uuid.UUID
) -> bool:
    """Capture one specific SAVED_STREAM asset identified by its UUID.

    Returns True on success, False if the asset row no longer exists or is
    not a SAVED_STREAM.  Raises on capture failure so the caller can leave
    the queue message undeleted.
    """
    async with session_factory() as db:
        result = await db.execute(select(Asset).where(Asset.id == asset_id))
        asset = result.scalar_one_or_none()
        if asset is None:
            logger.info("Asset %s no longer exists — skipping capture", asset_id)
            return False
        if asset.asset_type != AssetType.SAVED_STREAM:
            logger.info("Asset %s is not a SAVED_STREAM — skipping capture", asset_id)
            return False
        if asset.size_bytes > 0:
            logger.info("Asset %s already captured — skipping", asset_id)
            return True

        capture_path = await _capture_stream(asset, asset_dir, db)
        if capture_path is None:
            logger.error("Stream capture failed for asset %s", asset_id)
            return False
        logger.info("Stream capture complete for asset %s", asset_id)
        return True
