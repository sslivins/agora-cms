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

# Active transcode tracking — allows profile/asset updates to kill stale ffmpeg
_active_process = None  # asyncio.subprocess.Process | None
_active_profile_id: uuid.UUID | None = None
_active_source_asset_id: uuid.UUID | None = None
_active_variant_id: uuid.UUID | None = None
_cancelled_variant_ids: set[uuid.UUID] = set()


def cancel_profile_transcodes(profile_id: uuid.UUID) -> bool:
    """Kill the active ffmpeg process if it belongs to the given profile.

    Called from the profile update/delete endpoint when transcoding-relevant
    fields change or the profile is removed.  Returns True if a process was
    terminated.
    """
    global _active_process, _active_profile_id, _active_variant_id
    if (
        _active_process is not None
        and _active_profile_id == profile_id
        and _active_process.returncode is None  # still running
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
    """Kill the active ffmpeg process if it belongs to the given source asset.

    Called from the asset delete endpoint before removing variants.
    Returns True if a process was terminated.
    """
    global _active_process, _active_source_asset_id, _active_variant_id
    if (
        _active_process is not None
        and _active_source_asset_id == asset_id
        and _active_process.returncode is None  # still running
    ):
        logger.info(
            "Cancelling active transcode for asset %s (variant %s)",
            asset_id, _active_variant_id,
        )
        _cancelled_variant_ids.add(_active_variant_id)
        _active_process.terminate()
        return True
    return False

# Map profile video_codec value → ffmpeg encoder name
CODEC_ENCODER_MAP: dict[str, str] = {
    "h264": "libx264",
    "h265": "libx265",
    "av1": "libsvtav1",
}

# Map profile color_space value → ffmpeg colorspace, color_primaries, color_trc
COLOR_SPACE_MAP: dict[str, dict[str, str]] = {
    "bt709":      {"colorspace": "bt709",    "color_primaries": "bt709",  "color_trc": "bt709"},
    "smpte170m":  {"colorspace": "smpte170m","color_primaries": "smpte170m","color_trc": "smpte170m"},
    "bt2020-pq":  {"colorspace": "bt2020nc", "color_primaries": "bt2020", "color_trc": "smpte2084"},
    "bt2020-hlg": {"colorspace": "bt2020nc", "color_primaries": "bt2020", "color_trc": "arib-std-b67"},
}


# Transfer characteristics that indicate HDR content
_HDR_TRCS = {"smpte2084", "arib-std-b67"}

# Target color spaces that are HDR (no tone mapping needed when targeting these)
_HDR_TARGET_CS = {"bt2020-pq", "bt2020-hlg"}


def _build_ffmpeg_args_safe(
    source_path: Path,
    output_path: Path,
    profile: DeviceProfile,
    *,
    source_color_trc: str | None = None,
) -> list[str]:
    """Build ffmpeg command-line arguments for a given profile.

    Uses a robust scale filter that handles both scaling down and
    maintaining divisibility by 2 for H.264 encoding.
    """
    max_w = profile.max_width
    max_h = profile.max_height

    pix_fmt = profile.pixel_format or "auto"
    cs_key = profile.color_space or "auto"
    cs = COLOR_SPACE_MAP.get(cs_key) if cs_key != "auto" else None

    # Determine if we need HDR → SDR tone mapping:
    # Source is HDR (PQ or HLG) and target is not an HDR color space.
    needs_tonemap = (
        source_color_trc in _HDR_TRCS
        and cs_key not in _HDR_TARGET_CS
    )

    # When pixel format is "auto", force yuv420p for codecs/profiles that
    # only support 4:2:0 — passing through a 4:2:2 source would fail.
    # Also force yuv420p when tone-mapping HDR → SDR (output is always 8-bit).
    if pix_fmt == "auto":
        _420_only_profiles = {
            "h264": {"baseline", "main", "high", "high10"},
            "h265": {"main", "main10"},
        }
        restricted = _420_only_profiles.get(profile.video_codec, set())
        if needs_tonemap or profile.video_codec == "av1" or profile.video_profile in restricted:
            pix_fmt = "yuv420p"

    # When tone-mapping HDR → SDR, force BT.709 output regardless of profile setting
    if needs_tonemap:
        cs = COLOR_SPACE_MAP["bt709"]

    # Scale filter: only shrink (never upscale), maintain aspect ratio,
    # ensure dimensions are divisible by 2
    scale_parts = [
        f"scale=w='if(gt(iw,{max_w}),{max_w},iw)':h='if(gt(ih,{max_h}),{max_h},ih)'"
        f":force_original_aspect_ratio=decrease",
        "pad=ceil(iw/2)*2:ceil(ih/2)*2",
    ]

    # HDR → SDR tone-mapping filter chain (must come after scale, before format)
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
        "-i", str(source_path),
        "-c:v", encoder,
    ]

    # -profile:v only applies to H.264/H.265, not AV1
    if profile.video_profile and profile.video_codec in ("h264", "h265"):
        args.extend(["-profile:v", profile.video_profile])

    args.extend(["-vf", scale_filter])
    args.extend(["-r", str(profile.max_fps)])

    # Force color space when explicitly set (not pass-through)
    if cs:
        args.extend([
            "-colorspace", cs["colorspace"],
            "-color_primaries", cs["color_primaries"],
            "-color_trc", cs["color_trc"],
        ])

    if profile.video_bitrate:
        # Bitrate is stored as a number (Mbps) — append "M" for ffmpeg
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

    # Opus is not supported in MP4 containers — use MKV instead.
    # Only add -movflags +faststart for MP4 outputs.
    ext = output_path.suffix.lower()
    if ext != ".mkv":
        args.append("-movflags")
        args.append("+faststart")

    args.append(str(output_path))

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
        "frame_rate": None, "color_space": None, "color_transfer": None,
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
            ct = stream.get("color_transfer")
            if ct:
                result["color_transfer"] = ct
        elif codec_type == "audio" and result["audio_codec"] is None:
            result["audio_codec"] = stream.get("codec_name")

    # Map still-image codecs to friendly format names.
    # Codecs like mjpeg/png/bmp/tiff/webp are unambiguously images.
    # hevc and av1 are ambiguous (H.265 video vs HEIC image, AV1 video vs AVIF image)
    # so we also check the container format for those.
    _IMAGE_CODEC_MAP = {
        "mjpeg": "jpeg",
        "png": "png",
        "bmp": "bmp",
        "tiff": "tiff",
        "webp": "webp",
    }
    format_name = fmt.get("format_name", "")
    _IMAGE_CONTAINER_CODEC_MAP = {
        "hevc": ("heic", {"heif"}),
        "av1": ("avif", {"avif"}),
    }
    codec = result["video_codec"]
    if codec in _IMAGE_CODEC_MAP:
        result["video_codec"] = _IMAGE_CODEC_MAP[codec]
        result["frame_rate"] = None
        result["bitrate"] = None
        result["duration_seconds"] = None
    elif codec in _IMAGE_CONTAINER_CODEC_MAP:
        friendly, containers = _IMAGE_CONTAINER_CODEC_MAP[codec]
        # format_name can be comma-separated list, e.g. "mov,mp4,m4a,3gp,3g2,mj2"
        fmts = {f.strip() for f in format_name.split(",")}
        if fmts & containers:
            result["video_codec"] = friendly
            result["frame_rate"] = None
            result["bitrate"] = None
            result["duration_seconds"] = None

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

    # Track active transcode so profile/asset updates can cancel it
    global _active_process, _active_profile_id, _active_source_asset_id, _active_variant_id
    _active_profile_id = variant.profile_id
    _active_source_asset_id = variant.source_asset_id
    _active_variant_id = variant.id

    logger.info(
        "Transcoding %s → %s (profile: %s)",
        source.filename, variant.filename, profile.name,
    )

    # Image assets: convert/downscale to JPEG at profile max dimensions
    if source.asset_type == AssetType.IMAGE:
        try:
            ok = await convert_image_to_jpeg(
                source_path, output_path,
                max_width=profile.max_width,
                max_height=profile.max_height,
            )
            if not ok:
                variant.status = VariantStatus.FAILED
                variant.error_message = "Image conversion failed"
                variant.progress = 0.0
                await db.commit()
                return

            file_size = output_path.stat().st_size
            sha = hashlib.sha256()
            with open(output_path, "rb") as f:
                while chunk := f.read(1024 * 1024):
                    sha.update(chunk)
            variant.checksum = sha.hexdigest()
            variant.size_bytes = file_size
            variant.status = VariantStatus.READY
            variant.progress = 100.0
            variant.completed_at = datetime.now(timezone.utc)

            meta = await probe_media(output_path)
            for key, val in meta.items():
                if val is not None:
                    setattr(variant, key, val)

            await db.commit()
            logger.info("Image variant complete: %s (%d bytes)", variant.filename, variant.size_bytes)
            return
        except Exception as e:
            variant.status = VariantStatus.FAILED
            variant.error_message = str(e)[:500]
            variant.progress = 0.0
            await db.commit()
            logger.exception("Image variant error for %s", variant.filename)
            return

    # Video assets: full ffmpeg transcode
    # Get source duration for progress tracking
    duration = await _get_duration(source_path)

    # Probe source for HDR detection (tone mapping)
    source_meta = await probe_media(source_path)

    args = _build_ffmpeg_args_safe(
        source_path, output_path, profile,
        source_color_trc=source_meta.get("color_transfer"),
    )

    try:
        # Run ffmpeg with low priority
        env = os.environ.copy()
        proc = await asyncio.create_subprocess_exec(
            "nice", "-n", "15", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        _active_process = proc

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
        _active_process = None

        if proc.returncode != 0:
            # Check if this was a deliberate cancellation from a profile update
            if variant.id in _cancelled_variant_ids:
                _cancelled_variant_ids.discard(variant.id)
                logger.info("Transcode cancelled for %s (profile updated)", variant.filename)
                return
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
        _active_process = None
        variant.status = VariantStatus.FAILED
        variant.error_message = str(e)[:500]
        variant.progress = 0.0
        await db.commit()
        logger.exception("Transcode error for %s", variant.filename)


async def convert_image_to_jpeg(
    source_path: Path,
    output_path: Path,
    max_width: int | None = None,
    max_height: int | None = None,
) -> bool:
    """Convert an image file to JPEG using the best available tool.

    HEIC/HEIF files use heif-convert (handles grid-tiled images correctly),
    then ffmpeg to resize. Other formats use ffmpeg directly.
    When max_width/max_height are provided, images are capped at those
    dimensions (never upscaled). When omitted, original resolution is kept.
    """
    ext = source_path.suffix.lower()

    # Scale filter: shrink to fit max dimensions, never upscale
    vf_args: list[str] = []
    if max_width is not None and max_height is not None:
        scale_filter = (
            f"scale=w='min(iw,{max_width})':h='min(ih,{max_height})'"
            ":force_original_aspect_ratio=decrease"
        )
        vf_args = ["-vf", scale_filter]

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

        # ffmpeg: convert (+ optionally resize)
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y",
            "-i", str(ffmpeg_input),
            *vf_args,
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
    """Create pending variants for all video and image assets for a new profile.

    Returns the number of variants enqueued.
    """
    result = await db.execute(
        select(Asset).where(
            Asset.asset_type.in_([AssetType.VIDEO, AssetType.IMAGE])
        )
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
        if asset.asset_type == AssetType.IMAGE:
            ext = ".jpg"
        elif profile.audio_codec == "libopus":
            # Opus audio is not supported in MP4; use MKV container.
            ext = ".mkv"
        else:
            ext = ".mp4"
        variant = AssetVariant(
            id=variant_id,
            source_asset_id=asset.id,
            profile_id=profile_id,
            filename=f"{variant_id}{ext}",
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
