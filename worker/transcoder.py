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

from shared.models.asset import Asset, AssetType, AssetVariant, VariantStatus
from shared.models.device_profile import DeviceProfile
from shared.services.image import convert_image, image_variant_ext
from shared.services.probe import probe_media
from shared.services.storage import get_storage

logger = logging.getLogger("agora.worker.transcoder")


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

    ext = output_path.suffix.lower()
    if ext != ".mkv":
        args.append("-movflags")
        args.append("+faststart")

    args.append(str(output_path))

    return args


# ── Duration helper ─────────────────────────────────────────────

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


# ── Single variant transcoding ──────────────────────────────────

async def _transcode_one(variant: AssetVariant, db: AsyncSession, asset_dir: Path) -> None:
    """Transcode a single variant using ffmpeg."""
    await db.refresh(variant, ["source_asset", "profile"])
    source = variant.source_asset
    profile = variant.profile

    # Use original source file when available
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
        variant.status = VariantStatus.FAILED
        variant.error_message = "Source file not found"
        await db.commit()
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

            # Sync variant to cloud storage
            storage = get_storage()
            await storage.on_file_stored(f"variants/{variant.filename}")

            return
        except Exception as e:
            variant.status = VariantStatus.FAILED
            variant.error_message = str(e)[:500]
            variant.progress = 0.0
            await db.commit()
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
                    if pct - last_progress_update >= 1.0:
                        variant.progress = round(pct, 1)
                        await db.commit()
                        last_progress_update = pct

        await proc.wait()
        _active_process = None

        if proc.returncode != 0:
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

        # Success
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
        logger.info("Transcode complete: %s (%d bytes)", variant.filename, variant.size_bytes)

        # Sync variant to cloud storage
        storage = get_storage()
        await storage.on_file_stored(f"variants/{variant.filename}")

    except Exception as e:
        _active_process = None
        variant.status = VariantStatus.FAILED
        variant.error_message = str(e)[:500]
        variant.progress = 0.0
        await db.commit()
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


async def process_pending(session_factory, asset_dir: Path) -> int:
    """Process all pending variants. Returns number processed."""
    count = 0
    while True:
        async with session_factory() as db:
            result = await db.execute(
                select(AssetVariant)
                .where(AssetVariant.status == VariantStatus.PENDING)
                .order_by(AssetVariant.created_at)
                .limit(1)
            )
            variant = result.scalar_one_or_none()

            if variant is None:
                break

            await _transcode_one(variant, db, asset_dir)
            count += 1

    return count
