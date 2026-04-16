"""Stream URL probe endpoint — inspects HLS/DASH/RTMP/progressive streams."""

import asyncio
import logging
import re
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query

from cms.auth import require_auth

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/streams", dependencies=[Depends(require_auth)])

# Schemes we can HTTP-fetch playlists for
_HTTP_SCHEMES = ("http", "https")
# Schemes that are always "live" (no playlist to inspect)
_LIVE_ONLY_SCHEMES = ("rtmp", "rtmps", "rtsp", "rtsps", "mms", "mmsh")

_PROBE_TIMEOUT = 8.0  # seconds


def _parse_hls_master(text: str) -> list[dict]:
    """Parse an HLS master playlist, extracting variant stream info."""
    variants = []
    lines = text.strip().splitlines()
    for i, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF:"):
            attrs = line.split(":", 1)[1]
            variant: dict = {}

            # Resolution
            m = re.search(r"RESOLUTION=(\d+x\d+)", attrs)
            if m:
                variant["resolution"] = m.group(1)

            # Codecs
            m = re.search(r'CODECS="([^"]+)"', attrs)
            if m:
                variant["codecs"] = m.group(1)

            # Bandwidth
            m = re.search(r"BANDWIDTH=(\d+)", attrs)
            if m:
                variant["bandwidth_bps"] = int(m.group(1))

            # Average bandwidth
            m = re.search(r"AVERAGE-BANDWIDTH=(\d+)", attrs)
            if m:
                variant["avg_bandwidth_bps"] = int(m.group(1))

            # Frame rate
            m = re.search(r"FRAME-RATE=([\d.]+)", attrs)
            if m:
                variant["frame_rate"] = float(m.group(1))

            # URI is on the next non-comment line
            if i + 1 < len(lines) and not lines[i + 1].startswith("#"):
                variant["uri"] = lines[i + 1].strip()

            variants.append(variant)
    return variants


def _is_master_playlist(text: str) -> bool:
    """Check if HLS content is a master playlist (has STREAM-INF)."""
    return "#EXT-X-STREAM-INF:" in text


def _hls_is_live(text: str) -> bool:
    """A child HLS playlist is live if it lacks #EXT-X-ENDLIST."""
    return "#EXT-X-ENDLIST" not in text


def _hls_estimate_duration(text: str) -> Optional[float]:
    """Estimate total duration from EXTINF tags (VOD only)."""
    total = 0.0
    for m in re.finditer(r"#EXTINF:([\d.]+)", text):
        total += float(m.group(1))
    return round(total, 1) if total > 0 else None


def _friendly_codec(raw: str) -> str:
    """Convert codec string like 'avc1.64001f,mp4a.40.2' to friendly names."""
    parts = []
    for c in raw.split(","):
        c = c.strip()
        if c.startswith("avc1") or c.startswith("avc3"):
            parts.append("H.264")
        elif c.startswith("hvc1") or c.startswith("hev1"):
            parts.append("H.265")
        elif c.startswith("av01"):
            parts.append("AV1")
        elif c.startswith("vp09"):
            parts.append("VP9")
        elif c.startswith("mp4a"):
            parts.append("AAC")
        elif c.startswith("ac-3") or c.startswith("ec-3"):
            parts.append("AC-3")
        elif c.startswith("opus"):
            parts.append("Opus")
        else:
            parts.append(c)
    return " + ".join(parts)


async def _ffprobe_url(url: str) -> Optional[dict]:
    """Use ffprobe to inspect a non-HLS stream URL."""
    args = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_format", "-show_streams",
        "-timeout", str(int(_PROBE_TIMEOUT * 1_000_000)),  # microseconds
        url,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=_PROBE_TIMEOUT + 2)
        if proc.returncode != 0:
            return None

        import json
        data = json.loads(stdout.decode("utf-8", errors="replace"))

        result: dict = {"type": "progressive", "is_live": False}

        # Format info
        fmt = data.get("format", {})
        dur = fmt.get("duration")
        if dur:
            result["duration_seconds"] = round(float(dur), 1)

        bitrate = fmt.get("bit_rate")
        if bitrate:
            result["bitrate_bps"] = int(bitrate)

        # Stream info (first video + first audio)
        for s in data.get("streams", []):
            if s.get("codec_type") == "video" and "video_codec" not in result:
                result["video_codec"] = s.get("codec_name", "").upper()
                w, h = s.get("width"), s.get("height")
                if w and h:
                    result["resolution"] = f"{w}x{h}"
                fr = s.get("r_frame_rate", "")
                if "/" in fr:
                    num, den = fr.split("/")
                    if int(den) > 0:
                        result["frame_rate"] = round(int(num) / int(den), 2)
            elif s.get("codec_type") == "audio" and "audio_codec" not in result:
                result["audio_codec"] = s.get("codec_name", "").upper()

        return result
    except Exception as e:
        logger.warning("ffprobe failed for %s: %s", url, e)
        return None


@router.get("/probe")
async def probe_stream(url: str = Query(..., min_length=1)):
    """Probe a stream URL and return metadata (live/vod, resolution, codecs, etc.)."""
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()

    if not parsed.netloc:
        raise HTTPException(status_code=400, detail="Invalid URL")

    # RTMP/RTSP — always live, use ffprobe for metadata
    if scheme in _LIVE_ONLY_SCHEMES:
        result = {"url": url, "type": "rtmp_rtsp", "is_live": True}
        probe = await _ffprobe_url(url)
        if probe:
            for k in ("resolution", "video_codec", "audio_codec", "frame_rate", "bitrate_bps"):
                if k in probe:
                    result[k] = probe[k]
        return result

    if scheme not in _HTTP_SCHEMES:
        raise HTTPException(status_code=400, detail=f"Unsupported scheme: {scheme}")

    # Try to fetch the URL
    try:
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            content = resp.text
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Timed out fetching stream URL")
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502, detail=f"Stream URL returned HTTP {e.response.status_code}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch stream URL: {str(e)[:200]}")

    # Check if it's an HLS playlist
    if "#EXTM3U" in content[:100]:
        result: dict = {"url": url, "type": "hls"}

        if _is_master_playlist(content):
            variants = _parse_hls_master(content)
            result["variants"] = []

            for v in variants:
                vi: dict = {}
                if "resolution" in v:
                    vi["resolution"] = v["resolution"]
                if "codecs" in v:
                    vi["codecs_raw"] = v["codecs"]
                    vi["codecs"] = _friendly_codec(v["codecs"])
                if "bandwidth_bps" in v:
                    vi["bandwidth_kbps"] = round(v["bandwidth_bps"] / 1000)
                if "frame_rate" in v:
                    vi["frame_rate"] = v["frame_rate"]
                result["variants"].append(vi)

            # Best variant = highest bandwidth
            if variants:
                best = max(variants, key=lambda v: v.get("bandwidth_bps", 0))
                if "resolution" in best:
                    result["resolution"] = best["resolution"]
                if "codecs" in best:
                    result["codecs"] = _friendly_codec(best["codecs"])
                if "frame_rate" in best:
                    result["frame_rate"] = best["frame_rate"]

            # Fetch first child playlist to check live/vod
            first_uri = next((v.get("uri") for v in variants if v.get("uri")), None)
            if first_uri:
                child_url = urljoin(url, first_uri)
                try:
                    async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT, follow_redirects=True) as client:
                        child_resp = await client.get(child_url)
                        child_content = child_resp.text
                        result["is_live"] = _hls_is_live(child_content)
                        if not result["is_live"]:
                            dur = _hls_estimate_duration(child_content)
                            if dur:
                                result["duration_seconds"] = dur
                except Exception:
                    result["is_live"] = True  # assume live if we can't check

        else:
            # It's already a child/media playlist
            result["is_live"] = _hls_is_live(content)
            if not result["is_live"]:
                dur = _hls_estimate_duration(content)
                if dur:
                    result["duration_seconds"] = dur

        return result

    # Check if it's a DASH manifest
    if "<?xml" in content[:200] and ("<MPD" in content or "urn:mpeg:dash" in content):
        result = {"url": url, "type": "dash"}

        # Check for live: type="dynamic" means live
        result["is_live"] = 'type="dynamic"' in content

        # Try to extract duration
        m = re.search(r'mediaPresentationDuration="PT(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?"', content)
        if m:
            h = int(m.group(1) or 0)
            mins = int(m.group(2) or 0)
            secs = float(m.group(3) or 0)
            result["duration_seconds"] = round(h * 3600 + mins * 60 + secs, 1)

        # Extract resolution/codecs from AdaptationSet/Representation
        res_match = re.search(r'width="(\d+)".*?height="(\d+)"', content, re.DOTALL)
        if res_match:
            result["resolution"] = f"{res_match.group(1)}x{res_match.group(2)}"

        codec_match = re.search(r'codecs="([^"]+)"', content)
        if codec_match:
            result["codecs"] = _friendly_codec(codec_match.group(1))

        return result

    # Not HLS/DASH — try ffprobe (could be progressive MP4, MKV, etc.)
    probe = await _ffprobe_url(url)
    if probe:
        probe["url"] = url
        return probe

    # Couldn't determine anything
    return {"url": url, "type": "unknown", "is_live": None}
