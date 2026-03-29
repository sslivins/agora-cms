"""Asset transcoding service — converts video/images using ffmpeg.

Runs as a background task in the CMS process. Uses subprocess to call ffmpeg
with CPU-friendly settings (nice, limited concurrency) to avoid starving the
main CMS operations.
"""

import asyncio
import hashlib
import logging
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cms import database as _db
from cms.models.asset import Asset, AssetType, AssetVariant, VariantStatus
from cms.models.device_profile import DeviceProfile

logger = logging.getLogger("agora.cms.transcoder")

# Only one transcode at a time to avoid starving the CMS CPU
_transcode_semaphore = asyncio.Semaphore(1)

POLL_INTERVAL_SECONDS = 10


def _build_ffmpeg_args_safe(
    source_path: Path,
    output_path: Path,
    profile: DeviceProfile,
) -> list[str]:
    """Build ffmpeg command-line arguments for a given profile.

    Uses a robust scale filter that handles both scaling down and
    maintaining divisibility by 2 for H.264 encoding.
    """
    max_w = profile.max_width
    max_h = profile.max_height

    # Scale filter: only shrink (never upscale), maintain aspect ratio,
    # ensure dimensions are divisible by 2
    scale_filter = (
        f"scale=w='if(gt(iw,{max_w}),{max_w},iw)':h='if(gt(ih,{max_h}),{max_h},ih)'"
        f":force_original_aspect_ratio=decrease,"
        f"pad=ceil(iw/2)*2:ceil(ih/2)*2,"
        f"format=yuv420p"
    )

    args = [
        "ffmpeg", "-y",
        "-i", str(source_path),
        "-c:v", "libx264",
    ]

    if profile.video_profile:
        args.extend(["-profile:v", profile.video_profile])

    args.extend(["-vf", scale_filter])
    args.extend(["-r", str(profile.max_fps)])

    if profile.video_bitrate:
        args.extend(["-b:v", profile.video_bitrate])
    else:
        args.extend(["-crf", str(profile.crf)])

    args.extend([
        "-c:a", profile.audio_codec,
        "-b:a", profile.audio_bitrate,
        "-movflags", "+faststart",
        str(output_path),
    ])

    return args


async def _get_duration(source_path: Path) -> float | None:
    """Get duration of a media file in seconds using ffprobe."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(source_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        return float(stdout.decode().strip())
    except (ValueError, OSError):
        return None


async def _transcode_one(variant: AssetVariant, db: AsyncSession, asset_dir: Path) -> None:
    """Transcode a single variant using ffmpeg."""
    # Load related objects
    await db.refresh(variant, ["source_asset", "profile"])
    source = variant.source_asset
    profile = variant.profile

    source_path = asset_dir / source.filename
    variants_dir = asset_dir / "variants"
    variants_dir.mkdir(parents=True, exist_ok=True)
    output_path = variants_dir / variant.filename

    if not source_path.is_file():
        variant.status = VariantStatus.FAILED
        variant.error_message = "Source file not found"
        await db.commit()
        return

    # Mark as processing
    variant.status = VariantStatus.PROCESSING
    variant.progress = 0.0
    await db.commit()

    logger.info(
        "Transcoding %s → %s (profile: %s)",
        source.filename, variant.filename, profile.name,
    )

    # Get source duration for progress tracking
    duration = await _get_duration(source_path)

    args = _build_ffmpeg_args_safe(source_path, output_path, profile)

    try:
        # Run ffmpeg with low priority
        env = os.environ.copy()
        proc = await asyncio.create_subprocess_exec(
            "nice", "-n", "15", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        # Read stderr in chunks — ffmpeg uses \r for progress, not \n
        stderr_data = b""
        last_progress_update = 0.0
        while True:
            chunk = await proc.stderr.read(4096)
            if not chunk:
                break
            stderr_data += chunk

            # Parse progress from ffmpeg output: "time=00:01:23.45"
            if duration:
                decoded = chunk.decode("utf-8", errors="replace")
                matches = list(re.finditer(r"time=(\d+):(\d+):(\d+\.\d+)", decoded))
                if matches:
                    match = matches[-1]  # Use the latest time value
                    h, m, s = int(match.group(1)), int(match.group(2)), float(match.group(3))
                    elapsed = h * 3600 + m * 60 + s
                    pct = min(99.0, (elapsed / duration) * 100)
                    if pct - last_progress_update >= 1.0:  # Update at 1% intervals
                        variant.progress = round(pct, 1)
                        await db.commit()
                        last_progress_update = pct

        await proc.wait()

        if proc.returncode != 0:
            error_text = stderr_data.decode("utf-8", errors="replace")[-500:]
            variant.status = VariantStatus.FAILED
            variant.error_message = f"ffmpeg exit code {proc.returncode}: {error_text}"
            variant.progress = 0.0
            await db.commit()
            logger.error("Transcode failed for %s: exit %d", variant.filename, proc.returncode)
            return

        # Success — compute checksum and size
        file_size = output_path.stat().st_size
        sha = hashlib.sha256()
        with open(output_path, "rb") as f:
            while chunk := f.read(1024 * 1024):  # 1MB chunks
                sha.update(chunk)
        variant.checksum = sha.hexdigest()
        variant.size_bytes = file_size
        variant.status = VariantStatus.READY
        variant.progress = 100.0
        variant.completed_at = datetime.now(timezone.utc)
        await db.commit()
        logger.info("Transcode complete: %s (%d bytes)", variant.filename, variant.size_bytes)

    except Exception as e:
        variant.status = VariantStatus.FAILED
        variant.error_message = str(e)[:500]
        variant.progress = 0.0
        await db.commit()
        logger.exception("Transcode error for %s", variant.filename)


async def convert_image_to_jpeg(source_path: Path, output_path: Path) -> bool:
    """Convert an image file to JPEG using the best available tool.

    HEIC/HEIF files use heif-convert (handles grid-tiled images correctly).
    Other formats use ffmpeg.
    """
    ext = source_path.suffix.lower()

    try:
        if ext in (".heic", ".heif"):
            # heif-convert properly assembles grid tiles into a full image
            proc = await asyncio.create_subprocess_exec(
                "heif-convert", "-q", "92",
                str(source_path), str(output_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
            if proc.returncode == 0 and output_path.is_file():
                return True
            logger.warning("heif-convert failed for %s (exit %d), trying ffmpeg",
                           source_path.name, proc.returncode)

        # ffmpeg fallback (also primary path for AVIF, WebP, BMP, TIFF, GIF)
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y",
            "-i", str(source_path),
            "-frames:v", "1",
            "-update", "1",
            str(output_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        return proc.returncode == 0 and output_path.is_file()
    except OSError:
        logger.exception("Image conversion failed: %s", source_path)
        return False


async def enqueue_for_new_profile(profile_id, db: AsyncSession) -> int:
    """Create pending variants for all video assets for a new profile.

    Returns the number of variants enqueued.
    """
    result = await db.execute(
        select(Asset).where(Asset.asset_type == AssetType.VIDEO)
    )
    assets = result.scalars().all()

    profile_result = await db.execute(
        select(DeviceProfile).where(DeviceProfile.id == profile_id)
    )
    profile = profile_result.scalar_one_or_none()
    if not profile:
        return 0

    count = 0
    for asset in assets:
        # Check if variant already exists
        existing = await db.execute(
            select(AssetVariant).where(
                AssetVariant.source_asset_id == asset.id,
                AssetVariant.profile_id == profile_id,
            )
        )
        if existing.scalar_one_or_none():
            continue

        variant_filename = f"{Path(asset.filename).stem}_{profile.name}.mp4"
        variant = AssetVariant(
            source_asset_id=asset.id,
            profile_id=profile_id,
            filename=variant_filename,
        )
        db.add(variant)
        count += 1

    await db.commit()
    return count


async def transcoder_loop(asset_dir: Path) -> None:
    """Background loop that picks up pending variants and transcodes them."""
    logger.info("Transcoder started (poll interval=%ds)", POLL_INTERVAL_SECONDS)

    # Reset any variants left in PROCESSING state from a previous crash
    first_run = True

    while True:
        try:
            if _db._session_factory is None:
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
                continue

            if first_run:
                async with _db._session_factory() as db:
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
                first_run = False

            async with _transcode_semaphore:
                async with _db._session_factory() as db:
                    # Pick the oldest pending variant
                    result = await db.execute(
                        select(AssetVariant)
                        .where(AssetVariant.status == VariantStatus.PENDING)
                        .order_by(AssetVariant.created_at)
                        .limit(1)
                    )
                    variant = result.scalar_one_or_none()

                    if variant:
                        await _transcode_one(variant, db, asset_dir)
                    else:
                        await asyncio.sleep(POLL_INTERVAL_SECONDS)
                        continue

        except Exception:
            logger.exception("Transcoder loop error")
            await asyncio.sleep(POLL_INTERVAL_SECONDS)


def get_transcode_status() -> dict:
    """Quick status for dashboard — queries are done in the caller."""
    # This is a lightweight function; actual data comes from DB queries
    return {}
