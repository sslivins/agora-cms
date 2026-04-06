"""Tests for _build_ffmpeg_args_safe — validates ffmpeg command-line generation."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from cms.services.transcoder import _build_ffmpeg_args_safe


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


SRC = Path("/input/video.mp4")
OUT = Path("/output/video.mp4")


# ── Codec / encoder mapping ──────────────────────────────────────────

class TestCodecEncoder:
    def test_h264_uses_libx264(self):
        args = _build_ffmpeg_args_safe(SRC, OUT, _make_profile(video_codec="h264"))
        assert "-c:v" in args
        assert args[args.index("-c:v") + 1] == "libx264"

    def test_h265_uses_libx265(self):
        args = _build_ffmpeg_args_safe(SRC, OUT, _make_profile(video_codec="h265"))
        assert args[args.index("-c:v") + 1] == "libx265"

    def test_av1_uses_libsvtav1(self):
        args = _build_ffmpeg_args_safe(SRC, OUT, _make_profile(video_codec="av1"))
        assert args[args.index("-c:v") + 1] == "libsvtav1"

    def test_unknown_codec_falls_back_to_libx264(self):
        args = _build_ffmpeg_args_safe(SRC, OUT, _make_profile(video_codec="vp9"))
        assert args[args.index("-c:v") + 1] == "libx264"


# ── Video profile flag ────────────────────────────────────────────────

class TestVideoProfile:
    def test_h264_includes_profile_flag(self):
        args = _build_ffmpeg_args_safe(SRC, OUT, _make_profile(video_codec="h264", video_profile="high"))
        assert "-profile:v" in args
        assert args[args.index("-profile:v") + 1] == "high"

    def test_h265_includes_profile_flag(self):
        args = _build_ffmpeg_args_safe(SRC, OUT, _make_profile(video_codec="h265", video_profile="main"))
        assert "-profile:v" in args

    def test_av1_omits_profile_flag(self):
        args = _build_ffmpeg_args_safe(SRC, OUT, _make_profile(video_codec="av1", video_profile="main"))
        assert "-profile:v" not in args


# ── Pixel format (auto / explicit) ───────────────────────────────────

class TestPixelFormat:
    def test_auto_h264_no_format_filter(self):
        """Auto + H.264 should NOT add a format= filter (pass-through)."""
        args = _build_ffmpeg_args_safe(SRC, OUT, _make_profile(pixel_format="auto", video_codec="h264"))
        vf = args[args.index("-vf") + 1]
        assert "format=" not in vf

    def test_auto_av1_forces_yuv420p(self):
        """Auto + AV1 must force yuv420p because SVT-AV1 only supports 4:2:0."""
        args = _build_ffmpeg_args_safe(SRC, OUT, _make_profile(pixel_format="auto", video_codec="av1"))
        vf = args[args.index("-vf") + 1]
        assert "format=yuv420p" in vf

    def test_explicit_yuv422p(self):
        args = _build_ffmpeg_args_safe(SRC, OUT, _make_profile(pixel_format="yuv422p"))
        vf = args[args.index("-vf") + 1]
        assert "format=yuv422p" in vf

    def test_explicit_yuv420p10le(self):
        args = _build_ffmpeg_args_safe(SRC, OUT, _make_profile(pixel_format="yuv420p10le"))
        vf = args[args.index("-vf") + 1]
        assert "format=yuv420p10le" in vf

    def test_empty_string_treated_as_auto(self):
        """Empty pixel_format should behave like auto."""
        args = _build_ffmpeg_args_safe(SRC, OUT, _make_profile(pixel_format="", video_codec="h264"))
        vf = args[args.index("-vf") + 1]
        assert "format=" not in vf


# ── Color space (auto / explicit) ────────────────────────────────────

class TestColorSpace:
    def test_auto_no_colorspace_args(self):
        """Auto color space should not add setparams or colorspace flags."""
        args = _build_ffmpeg_args_safe(SRC, OUT, _make_profile(color_space="auto"))
        vf = args[args.index("-vf") + 1]
        assert "setparams" not in vf
        assert "-colorspace" not in args

    def test_bt709_adds_correct_args(self):
        args = _build_ffmpeg_args_safe(SRC, OUT, _make_profile(color_space="bt709"))
        vf = args[args.index("-vf") + 1]
        assert "setparams=colorspace=bt709:color_primaries=bt709:color_trc=bt709" in vf
        assert args[args.index("-colorspace") + 1] == "bt709"
        assert args[args.index("-color_primaries") + 1] == "bt709"
        assert args[args.index("-color_trc") + 1] == "bt709"

    def test_bt2020_pq_hdr10(self):
        args = _build_ffmpeg_args_safe(SRC, OUT, _make_profile(color_space="bt2020-pq"))
        assert args[args.index("-colorspace") + 1] == "bt2020nc"
        assert args[args.index("-color_primaries") + 1] == "bt2020"
        assert args[args.index("-color_trc") + 1] == "smpte2084"

    def test_bt2020_hlg(self):
        args = _build_ffmpeg_args_safe(SRC, OUT, _make_profile(color_space="bt2020-hlg"))
        assert args[args.index("-color_trc") + 1] == "arib-std-b67"

    def test_smpte170m(self):
        args = _build_ffmpeg_args_safe(SRC, OUT, _make_profile(color_space="smpte170m"))
        assert args[args.index("-colorspace") + 1] == "smpte170m"

    def test_empty_string_treated_as_auto(self):
        args = _build_ffmpeg_args_safe(SRC, OUT, _make_profile(color_space=""))
        assert "-colorspace" not in args


# ── Bitrate vs CRF ───────────────────────────────────────────────────

class TestBitrateCrf:
    def test_crf_when_no_bitrate(self):
        args = _build_ffmpeg_args_safe(SRC, OUT, _make_profile(video_bitrate="", crf=18))
        assert "-crf" in args
        assert args[args.index("-crf") + 1] == "18"
        assert "-b:v" not in args

    def test_bitrate_number_appends_M(self):
        """Numeric bitrate (from UI Mbps field) should get 'M' appended."""
        args = _build_ffmpeg_args_safe(SRC, OUT, _make_profile(video_bitrate="5"))
        assert "-b:v" in args
        assert args[args.index("-b:v") + 1] == "5M"
        assert "-crf" not in args

    def test_bitrate_decimal_appends_M(self):
        args = _build_ffmpeg_args_safe(SRC, OUT, _make_profile(video_bitrate="2.5"))
        assert args[args.index("-b:v") + 1] == "2.5M"

    def test_bitrate_already_has_suffix(self):
        """Legacy values like '5M' should pass through without double-suffix."""
        args = _build_ffmpeg_args_safe(SRC, OUT, _make_profile(video_bitrate="5M"))
        assert args[args.index("-b:v") + 1] == "5M"

    def test_bitrate_with_k_suffix(self):
        args = _build_ffmpeg_args_safe(SRC, OUT, _make_profile(video_bitrate="500k"))
        assert args[args.index("-b:v") + 1] == "500k"


# ── Scale / resolution / FPS ─────────────────────────────────────────

class TestScaleAndFps:
    def test_max_resolution_in_scale_filter(self):
        args = _build_ffmpeg_args_safe(SRC, OUT, _make_profile(max_width=1280, max_height=720))
        vf = args[args.index("-vf") + 1]
        assert "1280" in vf
        assert "720" in vf

    def test_fps_flag(self):
        args = _build_ffmpeg_args_safe(SRC, OUT, _make_profile(max_fps=24))
        assert "-r" in args
        assert args[args.index("-r") + 1] == "24"


# ── Audio ─────────────────────────────────────────────────────────────

class TestAudio:
    def test_audio_codec_and_bitrate(self):
        args = _build_ffmpeg_args_safe(SRC, OUT, _make_profile(audio_codec="aac", audio_bitrate="192k"))
        assert args[args.index("-c:a") + 1] == "aac"
        assert args[args.index("-b:a") + 1] == "192k"

    def test_opus_audio(self):
        args = _build_ffmpeg_args_safe(SRC, OUT, _make_profile(audio_codec="libopus"))
        assert args[args.index("-c:a") + 1] == "libopus"


# ── General structure ─────────────────────────────────────────────────

class TestGeneralStructure:
    def test_starts_with_ffmpeg(self):
        args = _build_ffmpeg_args_safe(SRC, OUT, _make_profile())
        assert args[0] == "ffmpeg"
        assert args[1] == "-y"

    def test_input_and_output_paths(self):
        args = _build_ffmpeg_args_safe(SRC, OUT, _make_profile())
        assert "-i" in args
        assert args[args.index("-i") + 1] == str(SRC)
        assert args[-1] == str(OUT)

    def test_movflags_faststart(self):
        args = _build_ffmpeg_args_safe(SRC, OUT, _make_profile())
        assert "-movflags" in args
        assert args[args.index("-movflags") + 1] == "+faststart"


# ── Combined scenarios ────────────────────────────────────────────────

class TestCombinedScenarios:
    def test_av1_auto_pix_bt2020_pq(self):
        """AV1 + auto pixel format + HDR10 color space."""
        args = _build_ffmpeg_args_safe(SRC, OUT, _make_profile(
            video_codec="av1", pixel_format="auto", color_space="bt2020-pq",
        ))
        vf = args[args.index("-vf") + 1]
        # Should force yuv420p AND set HDR10 color params
        assert "format=yuv420p" in vf
        assert "smpte2084" in vf
        assert args[args.index("-c:v") + 1] == "libsvtav1"
        assert "-profile:v" not in args

    def test_h264_explicit_yuv444_bt709_bitrate(self):
        """H.264 with explicit 444, BT.709, and bitrate."""
        args = _build_ffmpeg_args_safe(SRC, OUT, _make_profile(
            video_codec="h264", pixel_format="yuv444p",
            color_space="bt709", video_bitrate="8", crf=18,
        ))
        vf = args[args.index("-vf") + 1]
        assert "format=yuv444p" in vf
        assert "colorspace=bt709" in vf
        assert args[args.index("-b:v") + 1] == "8M"
        assert "-crf" not in args  # bitrate takes precedence

    def test_h265_auto_everything_crf(self):
        """H.265 with both auto pass-through and CRF."""
        args = _build_ffmpeg_args_safe(SRC, OUT, _make_profile(
            video_codec="h265", pixel_format="auto", color_space="auto", crf=20,
        ))
        vf = args[args.index("-vf") + 1]
        assert "format=" not in vf
        assert "setparams" not in vf
        assert "-colorspace" not in args
        assert args[args.index("-crf") + 1] == "20"
        assert args[args.index("-c:v") + 1] == "libx265"
        assert "-profile:v" in args  # H.265 still gets profile flag
