"""Deterministic generators for nightly test media fixtures.

Each generator caches to `tests/nightly/.fixtures_cache/` so we only pay the
cost once per checkout. Binary fixtures are intentionally *not* committed —
generating from pure python keeps the repo slim and avoids the usual dance
around Git LFS / binary diffs.

Dependencies:
- Pillow + pillow-heif for all four image types
- imageio-ffmpeg supplies a static ffmpeg binary for the MP4 generator
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw

try:  # pillow-heif optional at import time so unit tests that skip can load.
    import pillow_heif  # type: ignore
    pillow_heif.register_heif_opener()
    _HEIF_AVAILABLE = True
except ImportError:  # pragma: no cover - optional dep
    _HEIF_AVAILABLE = False

try:
    import imageio_ffmpeg  # type: ignore
    _FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
except ImportError:  # pragma: no cover - optional dep
    _FFMPEG = None


CACHE_DIR = Path(__file__).resolve().parents[1] / ".fixtures_cache"


def _ensure_cache() -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR


def _gradient_image(size: tuple[int, int]) -> Image.Image:
    """Non-uniform content so the transcoder actually has something to compress."""
    img = Image.new("RGB", size, color=(0, 0, 0))
    draw = ImageDraw.Draw(img)
    w, h = size
    for y in range(h):
        r = int(255 * (y / h))
        draw.line([(0, y), (w, y)], fill=(r, (y * 3) % 256, (w - y) % 256))
    draw.rectangle([(w // 4, h // 4), (3 * w // 4, 3 * h // 4)], outline=(255, 255, 255), width=3)
    draw.text((10, 10), "Agora Nightly", fill=(255, 255, 255))
    return img


def sample_jpeg(name: str = "sample.jpg") -> Path:
    p = _ensure_cache() / name
    if not p.exists():
        _gradient_image((800, 600)).save(p, format="JPEG", quality=85)
    return p


def sample_png(name: str = "sample.png") -> Path:
    p = _ensure_cache() / name
    if not p.exists():
        _gradient_image((640, 480)).save(p, format="PNG", optimize=True)
    return p


def sample_heic(name: str = "sample.heic") -> Path:
    if not _HEIF_AVAILABLE:
        raise RuntimeError(
            "pillow-heif is not installed; `pip install pillow-heif` to enable "
            "HEIC fixture generation."
        )
    p = _ensure_cache() / name
    if not p.exists():
        _gradient_image((640, 480)).save(p, format="HEIF", quality=80)
    return p


def sample_mp4(name: str = "sample.mp4", duration: int = 2) -> Path:
    if _FFMPEG is None:
        raise RuntimeError(
            "imageio-ffmpeg is not installed; `pip install imageio-ffmpeg` to "
            "enable MP4 fixture generation."
        )
    p = _ensure_cache() / name
    if not p.exists():
        cmd = [
            _FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi",
            "-i", f"testsrc=duration={duration}:size=320x240:rate=15",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-t", str(duration),
            str(p),
        ]
        subprocess.run(cmd, check=True, capture_output=True)
    return p


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()
