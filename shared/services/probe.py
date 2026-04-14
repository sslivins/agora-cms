"""Media probing via ffprobe — shared between CMS (upload metadata) and worker."""

import asyncio
import logging
from pathlib import Path

logger = logging.getLogger("agora.shared.probe")


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
        fmts = {f.strip() for f in format_name.split(",")}
        if fmts & containers:
            result["video_codec"] = friendly
            result["frame_rate"] = None
            result["bitrate"] = None
            result["duration_seconds"] = None

    return result
