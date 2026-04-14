"""Image conversion utilities — shared between CMS (upload) and worker."""

import asyncio
import logging
from pathlib import Path

logger = logging.getLogger("agora.shared.image")


def image_variant_ext(source_filename: str) -> str:
    """Return the correct file extension for an image variant.

    PNG sources keep .png to preserve transparency; everything else
    (JPEG, HEIC→JPG, AVIF→JPG, etc.) uses .jpg.
    """
    return ".png" if source_filename.lower().endswith(".png") else ".jpg"


async def convert_image(
    source_path: Path,
    output_path: Path,
    max_width: int | None = None,
    max_height: int | None = None,
) -> bool:
    """Convert an image file, preserving format when possible.

    Output format is determined by the output_path extension:
    - .png → lossless PNG output
    - .jpg/.jpeg → JPEG output (quality ~93, -q:v 2)

    HEIC/HEIF source files use heif-convert (handles grid-tiled images
    correctly), then ffmpeg to resize. Other formats use ffmpeg directly.
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

        # Determine output codec args based on output extension
        out_ext = output_path.suffix.lower()
        if out_ext == ".png":
            codec_args = ["-c:v", "png"]
        else:
            codec_args = ["-q:v", "2"]

        # ffmpeg: convert (+ optionally resize)
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y",
            "-i", str(ffmpeg_input),
            *vf_args,
            "-frames:v", "1",
            "-update", "1",
            *codec_args,
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


# Backward-compatible alias
convert_image_to_jpeg = convert_image
