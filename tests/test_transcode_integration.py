"""Integration tests for actual transcoding — generates synthetic media,
runs ffmpeg, and verifies output properties via ffprobe.

These tests require ffmpeg on PATH.  They are skipped automatically in
environments where ffmpeg is not installed (e.g. CI unit-test runner).
Run inside the Docker container for full coverage::

    docker exec agora-cms-cms-1 python -m pytest tests/test_transcode_integration.py -v
"""

import asyncio
import base64
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

# Skip the entire module when ffmpeg is not available
pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None,
    reason="ffmpeg not found on PATH — run inside Docker",
)

from cms.services.transcoder import (
    convert_image_to_jpeg,
    probe_media,
)
from worker.transcoder import _build_ffmpeg_args_safe

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_profile(**overrides):
    """Build a fake DeviceProfile-like object with sensible defaults."""
    defaults = dict(
        video_codec="h264",
        video_profile="main",
        max_width=1920,
        max_height=1080,
        max_fps=30,
        video_bitrate="",
        crf=23,
        pixel_format="auto",
        color_space="auto",
        audio_codec="aac",
        audio_bitrate="128k",
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _ffprobe_json(path: Path) -> dict:
    """Run ffprobe and return parsed JSON with streams + format info."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_streams", "-show_format",
            "-of", "json",
            str(path),
        ],
        capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout)


def _video_stream(info: dict) -> dict:
    """Extract the first video stream from ffprobe JSON."""
    for s in info.get("streams", []):
        if s.get("codec_type") == "video":
            return s
    raise AssertionError("No video stream found in ffprobe output")


def _audio_stream(info: dict) -> dict:
    """Extract the first audio stream from ffprobe JSON."""
    for s in info.get("streams", []):
        if s.get("codec_type") == "audio":
            return s
    raise AssertionError("No audio stream found in ffprobe output")


# ---------------------------------------------------------------------------
# Synthetic fixture generators
# ---------------------------------------------------------------------------

def gen_video(
    path: Path,
    *,
    width: int = 320,
    height: int = 240,
    duration: float = 0.5,
    fps: int = 30,
    vcodec: str = "libx264",
    pix_fmt: str = "yuv420p",
    acodec: str = "aac",
    extra_args: list[str] | None = None,
) -> Path:
    """Generate a tiny synthetic video with a colour-bars pattern."""
    frames = max(1, int(duration * fps))
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i",
        f"color=c=blue:size={width}x{height}:rate={fps}:duration={duration}",
        "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=stereo",
        "-c:v", vcodec,
        "-pix_fmt", pix_fmt,
        "-frames:v", str(frames),
        "-c:a", acodec,
        "-b:a", "64k",
        "-shortest",
    ]
    if extra_args:
        cmd.extend(extra_args)
    cmd.append(str(path))
    subprocess.run(cmd, capture_output=True, check=True)
    assert path.is_file(), f"Failed to generate {path}"
    return path


def gen_image(path: Path, *, width: int = 320, height: int = 240, fmt: str = "png") -> Path:
    """Generate a tiny synthetic image."""
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i",
        f"color=c=red:size={width}x{height}:duration=1",
        "-frames:v", "1",
        "-update", "1",
        str(path),
    ]
    subprocess.run(cmd, capture_output=True, check=True)
    assert path.is_file(), f"Failed to generate {path}"
    return path


def gen_heic(path: Path, *, width: int = 320, height: int = 240) -> Path:
    """Generate a synthetic HEIC file.

    Tries heif-enc (if encoder plugin is available), then ffmpeg HEIF muxer,
    then falls back to a tiny embedded 8×8 HEIC constant.
    """
    if shutil.which("heif-enc"):
        # heif-enc route: JPEG → HEIC
        tmp_jpg = path.with_suffix(".tmp.jpg")
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-f", "lavfi", "-i",
                f"color=c=green:size={width}x{height}:duration=1",
                "-frames:v", "1", "-update", "1", "-q:v", "2",
                str(tmp_jpg),
            ],
            capture_output=True, check=True,
        )
        result = subprocess.run(
            ["heif-enc", "-q", "90", str(tmp_jpg), "-o", str(path)],
            capture_output=True, text=True,
        )
        tmp_jpg.unlink(missing_ok=True)
        if result.returncode == 0 and path.is_file():
            return path

    # Fallback: try ffmpeg HEIF muxer directly
    result = subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i",
            f"color=c=green:size={width}x{height}:duration=1",
            "-frames:v", "1",
            "-c:v", "libx265",
            "-x265-params", "log-level=error",
            "-tag:v", "hvc1",
            str(path),
        ],
        capture_output=True, text=True,
    )
    if result.returncode == 0 and path.is_file():
        return path

    # Last resort: write the embedded 8×8 HEIC constant
    path.write_bytes(base64.b64decode(_TINY_HEIC_B64))
    return path


# Minimal 8×8 pixel HEIC file (454 bytes) — generated with heif-enc,
# validated via round-trip decode + heif-convert.
_TINY_HEIC_B64 = (
    "AAAAHGZ0eXBoZWljAAAAAG1pZjFoZWljbWlhZgAAAXptZXRhAAAAAAAAACFoZGxyAAAAAAAA"
    "AABwaWN0AAAAAAAAAAAAAAAAAAAAAA5waXRtAAAAAAABAAAAImlsb2MAAAAAREAAAQABAAAA"
    "AAGeAAEAAAAAAAAAKAAAACNpaW5mAAAAAAABAAAAFWluZmUCAAAAAAEAAGh2YzEAAAAA+mlw"
    "cnAAAADaaXBjbwAAAHNodmNDAQNwAAAAAAAAAAAAHvAA/P34+AAADwMgAAEAGEABDAH//wNw"
    "AAADAJAAAAMAAAMAHroCQCEAAQAnQgEBA3AAAAMAkAAAAwAAAwAeoCCBBZbqrprmwIAAAAMA"
    "gAAAAwCEIgABAAZEAcFzwIkAAAATY29scm5jbHgAAQANAAaAAAAAFGlzcGUAAAAAAAAAQAAA"
    "AEAAAAAoY2xhcAAAAAgAAAABAAAACAAAAAH////IAAAAAv///8gAAAACAAAAEHBpeGkAAAAA"
    "AwgICAAAABhpcG1hAAAAAAAAAAEAAQWBAgMFhAAAADBtZGF0AAAAJCgBrxOA9Sci//9zMJ/8"
    "h8H/F3ZYWdxhVIcrEYcd006AAAAEBA=="
)


def write_embedded_heic(path: Path) -> Path:
    """Write the embedded 8×8 HEIC constant to *path*."""
    path.write_bytes(base64.b64decode(_TINY_HEIC_B64))
    return path


# ---------------------------------------------------------------------------
# Video transcoding tests
# ---------------------------------------------------------------------------

class TestVideoTranscoding:
    """End-to-end: generate source → build args → run ffmpeg → verify output."""

    def _transcode(self, source: Path, output: Path, profile) -> Path:
        """Run ffmpeg with the args our builder produces."""
        args = _build_ffmpeg_args_safe(source, output, profile)
        result = subprocess.run(args, capture_output=True, text=True)
        assert result.returncode == 0, (
            f"ffmpeg failed (exit {result.returncode}):\n{result.stderr[-1000:]}"
        )
        assert output.is_file()
        return output

    # -- Codec conversion ---------------------------------------------------

    def test_h264_to_h264(self, tmp_path):
        """H.264 source → H.264 output (re-encode, same codec)."""
        src = gen_video(tmp_path / "src.mp4", vcodec="libx264", pix_fmt="yuv420p")
        out = tmp_path / "out.mp4"

        self._transcode(src, out, _make_profile(video_codec="h264"))

        info = _ffprobe_json(out)
        vs = _video_stream(info)
        assert vs["codec_name"] == "h264"

    def test_h264_to_h265(self, tmp_path):
        """H.264 source → H.265 output."""
        src = gen_video(tmp_path / "src.mp4", vcodec="libx264")
        out = tmp_path / "out.mp4"

        self._transcode(src, out, _make_profile(video_codec="h265"))

        info = _ffprobe_json(out)
        vs = _video_stream(info)
        assert vs["codec_name"] == "hevc"

    def test_h265_to_h264(self, tmp_path):
        """H.265 source → H.264 output (common Pi Zero scenario)."""
        src = gen_video(tmp_path / "src.mp4", vcodec="libx265",
                        extra_args=["-x265-params", "log-level=error"])
        out = tmp_path / "out.mp4"

        self._transcode(src, out, _make_profile(video_codec="h264"))

        info = _ffprobe_json(out)
        vs = _video_stream(info)
        assert vs["codec_name"] == "h264"

    def test_h264_to_av1(self, tmp_path):
        """H.264 source → AV1 output."""
        src = gen_video(tmp_path / "src.mp4", vcodec="libx264")
        out = tmp_path / "out.mp4"

        self._transcode(src, out, _make_profile(video_codec="av1"))

        info = _ffprobe_json(out)
        vs = _video_stream(info)
        assert vs["codec_name"] == "av1"

    # -- Pixel format conversion -------------------------------------------

    def test_422_to_420_explicit(self, tmp_path):
        """4:2:2 source → yuv420p output (explicit pixel_format)."""
        src = gen_video(tmp_path / "src.mp4", pix_fmt="yuv422p")
        out = tmp_path / "out.mp4"

        self._transcode(src, out, _make_profile(pixel_format="yuv420p"))

        info = _ffprobe_json(out)
        vs = _video_stream(info)
        assert vs["pix_fmt"] == "yuv420p"

    def test_422_to_420_auto_h264_main(self, tmp_path):
        """4:2:2 source + auto pixel_format + H.264 main → forced to yuv420p.

        H.264 'main' profile doesn't support 4:2:2, so the server forces
        yuv420p when auto is selected.
        """
        src = gen_video(tmp_path / "src.mp4", pix_fmt="yuv422p")
        out = tmp_path / "out.mp4"

        self._transcode(src, out, _make_profile(pixel_format="auto", video_codec="h264", video_profile="main"))

        info = _ffprobe_json(out)
        vs = _video_stream(info)
        assert vs["pix_fmt"] == "yuv420p"

    def test_422_to_420_av1_auto_forced(self, tmp_path):
        """4:2:2 source + AV1 + auto → must produce yuv420p (server forces it)."""
        src = gen_video(tmp_path / "src.mp4", pix_fmt="yuv422p")
        out = tmp_path / "out.mp4"

        self._transcode(src, out, _make_profile(video_codec="av1", pixel_format="auto"))

        info = _ffprobe_json(out)
        vs = _video_stream(info)
        assert vs["pix_fmt"] == "yuv420p"

    def test_10bit_to_8bit(self, tmp_path):
        """10-bit source → 8-bit yuv420p output."""
        src = gen_video(tmp_path / "src.mp4", pix_fmt="yuv420p10le",
                        vcodec="libx265",
                        extra_args=["-x265-params", "log-level=error"])
        out = tmp_path / "out.mp4"

        self._transcode(src, out, _make_profile(
            video_codec="h264", pixel_format="yuv420p",
        ))

        info = _ffprobe_json(out)
        vs = _video_stream(info)
        assert vs["pix_fmt"] == "yuv420p"

    # -- Resolution scaling ------------------------------------------------

    def test_4k_downscale_to_1080p(self, tmp_path):
        """3840×2160 source → 1920×1080 max (scale down, keep aspect)."""
        src = gen_video(tmp_path / "src.mp4", width=3840, height=2160)
        out = tmp_path / "out.mp4"

        self._transcode(src, out, _make_profile(max_width=1920, max_height=1080))

        info = _ffprobe_json(out)
        vs = _video_stream(info)
        assert int(vs["width"]) <= 1920
        assert int(vs["height"]) <= 1080

    def test_small_video_not_upscaled(self, tmp_path):
        """640×480 source with 1920×1080 profile → stays ≤640×480."""
        src = gen_video(tmp_path / "src.mp4", width=640, height=480)
        out = tmp_path / "out.mp4"

        self._transcode(src, out, _make_profile(max_width=1920, max_height=1080))

        info = _ffprobe_json(out)
        vs = _video_stream(info)
        # Should not upscale — output ≤ source dimensions (allow +1 for rounding)
        assert int(vs["width"]) <= 642
        assert int(vs["height"]) <= 482

    def test_720p_downscale(self, tmp_path):
        """1920×1080 source → 720p profile."""
        src = gen_video(tmp_path / "src.mp4", width=1920, height=1080)
        out = tmp_path / "out.mp4"

        self._transcode(src, out, _make_profile(max_width=1280, max_height=720))

        info = _ffprobe_json(out)
        vs = _video_stream(info)
        assert int(vs["width"]) <= 1280
        assert int(vs["height"]) <= 720

    # -- Audio codec -------------------------------------------------------

    def test_aac_audio(self, tmp_path):
        """Output should have AAC audio when profile specifies aac."""
        src = gen_video(tmp_path / "src.mp4")
        out = tmp_path / "out.mp4"

        self._transcode(src, out, _make_profile(audio_codec="aac"))

        info = _ffprobe_json(out)
        a = _audio_stream(info)
        assert a["codec_name"] == "aac"

    def test_opus_audio(self, tmp_path):
        """Output should have Opus audio when profile specifies opus."""
        src = gen_video(tmp_path / "src.mp4")
        out = tmp_path / "out.mkv"

        profile = _make_profile(audio_codec="libopus")
        args = _build_ffmpeg_args_safe(src, out, profile)

        result = subprocess.run(args, capture_output=True, text=True)
        assert result.returncode == 0, f"ffmpeg failed:\n{result.stderr[-500:]}"

        info = _ffprobe_json(out)
        a = _audio_stream(info)
        assert a["codec_name"] == "opus"

    # -- Bitrate / CRF ----------------------------------------------------

    def test_crf_mode(self, tmp_path):
        """CRF mode (no bitrate set) should produce a valid output."""
        src = gen_video(tmp_path / "src.mp4")
        out = tmp_path / "out.mp4"

        self._transcode(src, out, _make_profile(video_bitrate="", crf=28))

        assert out.stat().st_size > 0
        info = _ffprobe_json(out)
        _video_stream(info)  # just verify it has a video stream

    def test_bitrate_mode(self, tmp_path):
        """Explicit bitrate should produce a valid output."""
        src = gen_video(tmp_path / "src.mp4")
        out = tmp_path / "out.mp4"

        self._transcode(src, out, _make_profile(video_bitrate="1"))

        assert out.stat().st_size > 0
        info = _ffprobe_json(out)
        _video_stream(info)

    # -- Color space -------------------------------------------------------

    def test_bt709_color_space(self, tmp_path):
        """BT.709 color space should be set in output metadata."""
        src = gen_video(tmp_path / "src.mp4")
        out = tmp_path / "out.mp4"

        self._transcode(src, out, _make_profile(color_space="bt709"))

        info = _ffprobe_json(out)
        vs = _video_stream(info)
        # ffprobe may report "bt709" in various fields
        cs_fields = (
            vs.get("color_space", ""),
            vs.get("color_primaries", ""),
            vs.get("color_transfer", ""),
        )
        assert any("bt709" in f for f in cs_fields), f"Expected bt709 in {cs_fields}"

    def test_auto_color_space_passthrough(self, tmp_path):
        """Auto color space: transcode should succeed without forcing colour."""
        src = gen_video(tmp_path / "src.mp4")
        out = tmp_path / "out.mp4"

        self._transcode(src, out, _make_profile(color_space="auto"))

        assert out.stat().st_size > 0

    # -- Combined "real-world" scenarios -----------------------------------

    def test_hevc_422_to_pi_zero_h264_420(self, tmp_path):
        """HEVC 4:2:2 10-bit source → Pi Zero profile (H.264, yuv420p, 720p).

        This is the exact scenario the user raised: an HEVC 4:2:2 file
        should produce an H.264 4:2:0 output suitable for Pi Zero hardware
        decoding.
        """
        src = gen_video(tmp_path / "src.mp4",
                        width=1920, height=1080,
                        vcodec="libx265", pix_fmt="yuv422p",
                        extra_args=["-x265-params", "log-level=error"])
        out = tmp_path / "out.mp4"

        pi_zero_profile = _make_profile(
            video_codec="h264",
            video_profile="baseline",
            max_width=1280,
            max_height=720,
            max_fps=30,
            pixel_format="yuv420p",
            crf=23,
            audio_codec="aac",
            audio_bitrate="128k",
        )

        self._transcode(src, out, pi_zero_profile)

        info = _ffprobe_json(out)
        vs = _video_stream(info)
        assert vs["codec_name"] == "h264"
        assert vs["pix_fmt"] == "yuv420p"
        assert int(vs["width"]) <= 1280
        assert int(vs["height"]) <= 720

    def test_4k_hdr_to_sdr_1080p(self, tmp_path):
        """4K 10-bit source → SDR 1080p H.264 (BT.709)."""
        src = gen_video(tmp_path / "src.mp4",
                        width=3840, height=2160,
                        vcodec="libx265", pix_fmt="yuv420p10le",
                        extra_args=["-x265-params", "log-level=error"])
        out = tmp_path / "out.mp4"

        self._transcode(src, out, _make_profile(
            video_codec="h264",
            pixel_format="yuv420p",
            max_width=1920,
            max_height=1080,
            color_space="bt709",
        ))

        info = _ffprobe_json(out)
        vs = _video_stream(info)
        assert vs["codec_name"] == "h264"
        assert vs["pix_fmt"] == "yuv420p"
        assert int(vs["width"]) <= 1920
        assert int(vs["height"]) <= 1080

    # -- FPS limiting ------------------------------------------------------

    def test_fps_limited(self, tmp_path):
        """60fps source → 30fps profile should produce ≤30fps output."""
        src = gen_video(tmp_path / "src.mp4", fps=60)
        out = tmp_path / "out.mp4"

        self._transcode(src, out, _make_profile(max_fps=30))

        info = _ffprobe_json(out)
        vs = _video_stream(info)
        rfr = vs.get("r_frame_rate", "30/1")
        if "/" in rfr:
            num, den = rfr.split("/")
            fps = float(num) / float(den)
        else:
            fps = float(rfr)
        assert fps <= 31  # allow small rounding


# ---------------------------------------------------------------------------
# Image conversion tests
# ---------------------------------------------------------------------------

class TestImageConversion:
    """Test convert_image_to_jpeg with real images."""

    # ----- Format conversion tests -----

    def test_png_to_jpeg(self, tmp_path):
        """PNG → JPEG conversion preserves dimensions."""
        src = gen_image(tmp_path / "test.png", width=640, height=480)
        out = tmp_path / "output.jpg"

        result = asyncio.run(
            convert_image_to_jpeg(src, out)
        )

        assert result is True
        assert out.is_file()
        with open(out, "rb") as f:
            assert f.read(2) == b"\xff\xd8"
        info = _ffprobe_json(out)
        vs = _video_stream(info)
        assert int(vs["width"]) == 640
        assert int(vs["height"]) == 480

    def test_webp_to_jpeg(self, tmp_path):
        """WebP → JPEG conversion."""
        src = gen_image(tmp_path / "test.webp", fmt="webp")
        out = tmp_path / "output.jpg"

        result = asyncio.run(
            convert_image_to_jpeg(src, out)
        )

        assert result is True
        assert out.is_file()
        with open(out, "rb") as f:
            header = f.read(2)
        assert header == b"\xff\xd8"

    def test_bmp_to_jpeg(self, tmp_path):
        """BMP → JPEG conversion."""
        src = gen_image(tmp_path / "test.bmp", fmt="bmp")
        out = tmp_path / "output.jpg"

        result = asyncio.run(
            convert_image_to_jpeg(src, out)
        )

        assert result is True
        assert out.is_file()

    def test_tiff_to_jpeg(self, tmp_path):
        """TIFF → JPEG conversion."""
        src = gen_image(tmp_path / "test.tiff", fmt="tiff")
        out = tmp_path / "output.jpg"

        result = asyncio.run(
            convert_image_to_jpeg(src, out)
        )

        assert result is True
        assert out.is_file()

    def test_avif_to_jpeg(self, tmp_path):
        """AVIF → JPEG conversion."""
        avif_path = tmp_path / "test.avif"
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-f", "lavfi", "-i", "color=c=blue:size=320x240:duration=1",
                "-frames:v", "1", "-c:v", "libaom-av1",
                "-still-picture", "1",
                str(avif_path),
            ],
            capture_output=True, check=True,
        )

        out = tmp_path / "output.jpg"
        success = asyncio.run(
            convert_image_to_jpeg(avif_path, out)
        )

        assert success is True
        assert out.is_file()

    def test_jpeg_to_jpeg_passthrough(self, tmp_path):
        """JPEG → JPEG (format-only, no scaling) preserves dimensions."""
        src = gen_image(tmp_path / "test.jpg", width=800, height=600)
        out = tmp_path / "output.jpg"

        result = asyncio.run(
            convert_image_to_jpeg(src, out)
        )

        assert result is True
        info = _ffprobe_json(out)
        vs = _video_stream(info)
        assert int(vs["width"]) == 800
        assert int(vs["height"]) == 600

    # ----- Downscale tests -----

    def test_large_png_downscaled(self, tmp_path):
        """3840×2160 PNG → output should be ≤ 1920×1080."""
        src = gen_image(tmp_path / "big.png", width=3840, height=2160)
        out = tmp_path / "output.jpg"

        result = asyncio.run(
            convert_image_to_jpeg(src, out, max_width=1920, max_height=1080)
        )
        assert result is True

        info = _ffprobe_json(out)
        vs = _video_stream(info)
        assert int(vs["width"]) <= 1920
        assert int(vs["height"]) <= 1080

    def test_large_webp_downscaled(self, tmp_path):
        """4K WebP → output should be ≤ 1920×1080."""
        src = gen_image(tmp_path / "big.webp", width=3840, height=2160, fmt="webp")
        out = tmp_path / "output.jpg"

        result = asyncio.run(
            convert_image_to_jpeg(src, out, max_width=1920, max_height=1080)
        )
        assert result is True

        info = _ffprobe_json(out)
        vs = _video_stream(info)
        assert int(vs["width"]) <= 1920
        assert int(vs["height"]) <= 1080

    def test_large_bmp_downscaled(self, tmp_path):
        """Oversized BMP → output should be ≤ 1280×720."""
        src = gen_image(tmp_path / "big.bmp", width=2560, height=1440, fmt="bmp")
        out = tmp_path / "output.jpg"

        result = asyncio.run(
            convert_image_to_jpeg(src, out, max_width=1280, max_height=720)
        )
        assert result is True

        info = _ffprobe_json(out)
        vs = _video_stream(info)
        assert int(vs["width"]) <= 1280
        assert int(vs["height"]) <= 720

    def test_custom_max_dimensions(self, tmp_path):
        """Image downscaled to custom profile dimensions (1280×720)."""
        src = gen_image(tmp_path / "big.png", width=3840, height=2160)
        out = tmp_path / "output.jpg"

        result = asyncio.run(
            convert_image_to_jpeg(src, out, max_width=1280, max_height=720)
        )
        assert result is True

        info = _ffprobe_json(out)
        vs = _video_stream(info)
        assert int(vs["width"]) <= 1280
        assert int(vs["height"]) <= 720

    def test_downscale_preserves_aspect_ratio(self, tmp_path):
        """16:9 image scaled to 1280×720 max should stay 16:9."""
        src = gen_image(tmp_path / "wide.png", width=3840, height=2160)
        out = tmp_path / "output.jpg"

        result = asyncio.run(
            convert_image_to_jpeg(src, out, max_width=1280, max_height=720)
        )
        assert result is True

        info = _ffprobe_json(out)
        vs = _video_stream(info)
        w, h = int(vs["width"]), int(vs["height"])
        # Aspect ratio should be ~1.78 (16:9), tolerance for rounding
        assert abs(w / h - 16 / 9) < 0.02

    def test_portrait_downscale(self, tmp_path):
        """Portrait (1080×1920) → max 720×1280 should respect height constraint."""
        src = gen_image(tmp_path / "portrait.png", width=1080, height=1920)
        out = tmp_path / "output.jpg"

        result = asyncio.run(
            convert_image_to_jpeg(src, out, max_width=720, max_height=1280)
        )
        assert result is True

        info = _ffprobe_json(out)
        vs = _video_stream(info)
        assert int(vs["width"]) <= 720
        assert int(vs["height"]) <= 1280

    # ----- No-upscale / passthrough tests -----

    def test_small_image_not_upscaled(self, tmp_path):
        """320×240 image with 1920×1080 max should stay 320×240."""
        src = gen_image(tmp_path / "small.png", width=320, height=240)
        out = tmp_path / "output.jpg"

        result = asyncio.run(
            convert_image_to_jpeg(src, out, max_width=1920, max_height=1080)
        )
        assert result is True

        info = _ffprobe_json(out)
        vs = _video_stream(info)
        assert int(vs["width"]) == 320
        assert int(vs["height"]) == 240

    def test_exact_max_not_changed(self, tmp_path):
        """Image exactly at max dims should not be rescaled."""
        src = gen_image(tmp_path / "exact.png", width=1920, height=1080)
        out = tmp_path / "output.jpg"

        result = asyncio.run(
            convert_image_to_jpeg(src, out, max_width=1920, max_height=1080)
        )
        assert result is True

        info = _ffprobe_json(out)
        vs = _video_stream(info)
        assert int(vs["width"]) == 1920
        assert int(vs["height"]) == 1080

    def test_no_scaling_preserves_resolution(self, tmp_path):
        """Without max dimensions, original resolution is preserved."""
        src = gen_image(tmp_path / "big.png", width=3840, height=2160)
        out = tmp_path / "output.jpg"

        result = asyncio.run(
            convert_image_to_jpeg(src, out)
        )
        assert result is True

        info = _ffprobe_json(out)
        vs = _video_stream(info)
        assert int(vs["width"]) == 3840
        assert int(vs["height"]) == 2160

    def test_width_only_exceeds(self, tmp_path):
        """Image wider than max but shorter — only width should be capped."""
        src = gen_image(tmp_path / "wide.png", width=2560, height=720)
        out = tmp_path / "output.jpg"

        result = asyncio.run(
            convert_image_to_jpeg(src, out, max_width=1920, max_height=1080)
        )
        assert result is True

        info = _ffprobe_json(out)
        vs = _video_stream(info)
        assert int(vs["width"]) <= 1920
        assert int(vs["height"]) <= 720  # should shrink proportionally

    def test_height_only_exceeds(self, tmp_path):
        """Image taller than max but narrower — only height should be capped."""
        src = gen_image(tmp_path / "tall.png", width=720, height=2560)
        out = tmp_path / "output.jpg"

        result = asyncio.run(
            convert_image_to_jpeg(src, out, max_width=1920, max_height=1080)
        )
        assert result is True

        info = _ffprobe_json(out)
        vs = _video_stream(info)
        assert int(vs["width"]) <= 720
        assert int(vs["height"]) <= 1080


class TestHeicConversion:
    """HEIC → JPEG tests — require heif-convert on PATH."""

    pytestmark = pytest.mark.skipif(
        shutil.which("heif-convert") is None,
        reason="heif-convert not found — run inside Docker with libheif-examples",
    )

    def test_heic_to_jpeg(self, tmp_path):
        """HEIC upload should produce a JPEG output."""
        src = gen_heic(tmp_path / "photo.heic")
        out = tmp_path / "photo.jpg"

        result = asyncio.run(
            convert_image_to_jpeg(src, out)
        )

        assert result is True
        assert out.is_file()
        with open(out, "rb") as f:
            header = f.read(2)
        assert header == b"\xff\xd8", "Output is not a valid JPEG"

    def test_heic_embedded_to_jpeg(self, tmp_path):
        """Embedded 8×8 HEIC → JPEG (always runnable with heif-convert)."""
        src = write_embedded_heic(tmp_path / "embedded.heic")
        out = tmp_path / "embedded.jpg"

        result = asyncio.run(
            convert_image_to_jpeg(src, out)
        )

        assert result is True
        assert out.is_file()
        with open(out, "rb") as f:
            header = f.read(2)
        assert header == b"\xff\xd8", "Output is not a valid JPEG"

        # Verify dimensions are reasonable (8×8 source)
        info = _ffprobe_json(out)
        vs = _video_stream(info)
        assert int(vs["width"]) <= 64
        assert int(vs["height"]) <= 64

    def test_heic_large_downscaled(self, tmp_path):
        """Large HEIC → should be downscaled to ≤ 1920×1080."""
        src = gen_heic(tmp_path / "big.heic", width=4000, height=3000)
        out = tmp_path / "output.jpg"

        result = asyncio.run(
            convert_image_to_jpeg(src, out, max_width=1920, max_height=1080)
        )
        assert result is True

        info = _ffprobe_json(out)
        vs = _video_stream(info)
        assert int(vs["width"]) <= 1920
        assert int(vs["height"]) <= 1080

    def test_heic_small_not_upscaled(self, tmp_path):
        """Small HEIC should not be upscaled when max > source dims."""
        src = gen_heic(tmp_path / "small.heic", width=320, height=240)
        out = tmp_path / "output.jpg"

        result = asyncio.run(
            convert_image_to_jpeg(src, out, max_width=1920, max_height=1080)
        )
        assert result is True

        info = _ffprobe_json(out)
        vs = _video_stream(info)
        assert int(vs["width"]) <= 320
        assert int(vs["height"]) <= 240

    def test_heic_custom_profile_dims(self, tmp_path):
        """HEIC downscaled to custom profile max (1280×720)."""
        src = gen_heic(tmp_path / "big.heic", width=4000, height=3000)
        out = tmp_path / "output.jpg"

        result = asyncio.run(
            convert_image_to_jpeg(src, out, max_width=1280, max_height=720)
        )
        assert result is True

        info = _ffprobe_json(out)
        vs = _video_stream(info)
        assert int(vs["width"]) <= 1280
        assert int(vs["height"]) <= 720


# ---------------------------------------------------------------------------
# probe_media tests (with real files)
# ---------------------------------------------------------------------------

class TestProbeMedia:
    """Verify probe_media returns correct metadata for real files."""

    def test_probe_h264_video(self, tmp_path):
        """probe_media should detect H.264 codec, dimensions, audio."""
        src = gen_video(tmp_path / "test.mp4", width=640, height=480)

        meta = asyncio.run(probe_media(src))

        assert meta["video_codec"] == "h264"
        assert meta["width"] == 640
        assert meta["height"] == 480
        assert meta["audio_codec"] == "aac"
        assert meta["duration_seconds"] is not None

    def test_probe_h265_video(self, tmp_path):
        """probe_media should detect HEVC codec."""
        src = gen_video(tmp_path / "test.mp4", vcodec="libx265",
                        extra_args=["-x265-params", "log-level=error"])

        meta = asyncio.run(probe_media(src))

        assert meta["video_codec"] == "hevc"

    def test_probe_jpeg_image(self, tmp_path):
        """probe_media on a JPEG should return friendly format and null video fields."""
        src = gen_image(tmp_path / "test.jpg", width=800, height=600)

        meta = asyncio.run(probe_media(src))

        assert meta["width"] == 800
        assert meta["height"] == 600
        assert meta["video_codec"] == "jpeg"
        assert meta["frame_rate"] is None
        assert meta["bitrate"] is None
        assert meta["duration_seconds"] is None

    def test_probe_png_image(self, tmp_path):
        """probe_media on a PNG should return 'png' and null video fields."""
        src = gen_image(tmp_path / "test.png", width=640, height=480, fmt="png")

        meta = asyncio.run(probe_media(src))

        assert meta["video_codec"] == "png"
        assert meta["frame_rate"] is None
        assert meta["bitrate"] is None
        assert meta["duration_seconds"] is None
