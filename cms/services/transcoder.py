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
import uuid
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

# Map profile video_codec value → ffmpeg encoder name
CODEC_ENCODER_MAP: dict[str, str] = {
    "h264": "libx264",
    "h265": "libx265",
}


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

    pix_fmt = profile.pixel_format or "yuv420p"
    cs = profile.color_space or "bt709"

    # Scale filter: only shrink (never upscale), maintain aspect ratio,
    # ensure dimensions are divisible by 2
    # Force pixel format and color space from profile
    scale_filter = (
        f"scale=w='if(gt(iw,{max_w}),{max_w},iw)':h='if(gt(ih,{max_h}),{max_h},ih)'"
        f":force_original_aspect_ratio=decrease,"
        f"pad=ceil(iw/2)*2:ceil(ih/2)*2,"
        f"format={pix_fmt},"
        f"setparams=colorspace={cs}:color_primaries={cs}:color_trc={cs}"
    )

    encoder = CODEC_ENCODER_MAP.get(profile.video_codec, "libx264")

    args = [
        "ffmpeg", "-y",
        "-i", str(source_path),
        "-c:v", encoder,
    ]

    if profile.video_profile:
        args.extend(["-profile:v", profile.video_profile])

    args.extend(["-vf", scale_filter])
    args.extend(["-r", str(profile.max_fps)])

    # Force color space from profile
    args.extend([
        "-colorspace", cs,
        "-color_primaries", cs,
        "-color_trc", cs,
    ])

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


async def probe_media(file_path: Path) -> dict:
    """Extract media metadata from a file using ffprobe. Returns a dict with
    width, height, duration_seconds, video_codec, audio_codec, bitrate,
    frame_rate, color_space — any of which may be None."""
    import json as _json

    result: dict = {
        "width": None, "height": None, "duration_seconds": None,
        "video_codec": None, "audio_codec": None, "bitrate": None,
        "frame_rate": None, "color_space": None,
    }
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error",
            "-show_entries",
            "stream=codec_type,codec_name,width,height,r_frame_rate,bit_rate,color_space,color_transfer,color_primaries",
            "-show_entries", "format=duration,bit_rate",
            "-of", "json",
            str(file_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        data = _json.loads(stdout.decode())
    except (OSError, ValueError, _json.JSONDecodeError):
        return result

    # Format-level info
    fmt = data.get("format", {})
    try:
        result["duration_seconds"] = float(fmt.get("duration", 0)) or None
    except (ValueError, TypeError):
        pass
    try:
        result["bitrate"] = int(fmt.get("bit_rate", 0)) or None
    except (ValueError, TypeError):
        pass

    # Stream-level info
    for stream in data.get("streams", []):
        codec_type = stream.get("codec_type")
        if codec_type == "video" and result["video_codec"] is None:
            result["video_codec"] = stream.get("codec_name")
            result["width"] = stream.get("width")
            result["height"] = stream.get("height")
            # Frame rate: r_frame_rate is like "30/1" or "30000/1001"
            rfr = stream.get("r_frame_rate", "")
            if "/" in rfr:
                num, den = rfr.split("/", 1)
                try:
                    fps = float(num) / float(den)
                    result["frame_rate"] = f"{fps:.2f}".rstrip("0").rstrip(".")
                except (ValueError, ZeroDivisionError):
                    pass
            # Color space
            cs = stream.get("color_space") or stream.get("color_primaries") or stream.get("color_transfer")
            if cs:
                result["color_space"] = cs
        elif codec_type == "audio" and result["audio_codec"] is None:
            result["audio_codec"] = stream.get("codec_name")

    return result


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

        # Probe variant media metadata
        meta = await probe_media(output_path)
        for key, val in meta.items():
            if val is not None:
                setattr(variant, key, val)

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

    HEIC/HEIF files use heif-convert (handles grid-tiled images correctly),
    then ffmpeg to resize. Other formats use ffmpeg directly.
    All images are capped at 1920×1080 for device compatibility.
    """
    ext = source_path.suffix.lower()

    # Scale filter: shrink to fit 1920×1080, never upscale
    scale_filter = (
        "scale=w='min(iw,1920)':h='min(ih,1080)'"
        ":force_original_aspect_ratio=decrease"
    )

    try:
        ffmpeg_input = source_path

        if ext in (".heic", ".heif"):
            # heif-convert properly assembles grid tiles into a full image
            heif_tmp = output_path.with_suffix(".heif_tmp.jpg")
            proc = await asyncio.create_subprocess_exec(
                "heif-convert", "-q", "92",
                str(source_path), str(heif_tmp),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
            if proc.returncode == 0 and heif_tmp.is_file():
                ffmpeg_input = heif_tmp
            else:
                logger.warning("heif-convert failed for %s (exit %d), trying ffmpeg directly",
                               source_path.name, proc.returncode)

        # ffmpeg: convert + resize to 1920×1080 max
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y",
            "-i", str(ffmpeg_input),
            "-vf", scale_filter,
            "-frames:v", "1",
            "-update", "1",
            "-q:v", "2",
            str(output_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()

        # Clean up heif temp file
        heif_tmp = output_path.with_suffix(".heif_tmp.jpg")
        if heif_tmp.is_file():
            heif_tmp.unlink()

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

        variant_id = uuid.uuid4()
        variant = AssetVariant(
            id=variant_id,
            source_asset_id=asset.id,
            profile_id=profile_id,
            filename=f"{variant_id}.mp4",
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
