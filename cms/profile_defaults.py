"""Canonical defaults for built-in device profiles.

Shared between _seed_profiles (startup) and the reset-to-defaults endpoint.
"""

BUILTIN_PROFILES = {
    "pi-zero-2w": {
        "description": "Raspberry Pi Zero 2 W — H.264 Main, 1080p30",
        "video_codec": "h264",
        "video_profile": "main",
        "max_width": 1920,
        "max_height": 1080,
        "max_fps": 30,
        "crf": 23,
        "video_bitrate": "",
        "pixel_format": "auto",
        "color_space": "auto",
        "audio_codec": "aac",
        "audio_bitrate": "128k",
    },
    "pi-4": {
        "description": "Raspberry Pi 4 — HEVC Main, 1080p30",
        "video_codec": "h265",
        "video_profile": "main",
        "max_width": 1920,
        "max_height": 1080,
        "max_fps": 30,
        "crf": 23,
        "video_bitrate": "",
        "pixel_format": "auto",
        "color_space": "auto",
        "audio_codec": "aac",
        "audio_bitrate": "128k",
    },
    "pi-5": {
        "description": "Raspberry Pi 5 / CM5 — HEVC Main, 1080p60",
        "video_codec": "h265",
        "video_profile": "main",
        "max_width": 1920,
        "max_height": 1080,
        "max_fps": 60,
        "crf": 23,
        "video_bitrate": "",
        "pixel_format": "auto",
        "color_space": "auto",
        "audio_codec": "aac",
        "audio_bitrate": "128k",
    },
}
