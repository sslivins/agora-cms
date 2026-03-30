"""Periodically check GitHub for the latest Agora device release."""

import asyncio
import logging
from typing import Optional

import httpx

logger = logging.getLogger("agora.cms.version_checker")

GITHUB_REPO = "sslivins/agora"
CHECK_INTERVAL = 1800  # 30 minutes

_latest_version: Optional[str] = None


def get_latest_device_version() -> Optional[str]:
    """Return the cached latest device version, or None if unknown."""
    return _latest_version


def _parse_version(v: str) -> tuple:
    """Parse a version string like '0.7.3' into a comparable tuple of ints."""
    try:
        return tuple(int(x) for x in v.split("."))
    except (ValueError, AttributeError):
        return ()


def is_update_available(device_version: str, latest: Optional[str] = None) -> bool:
    """Return True only if the device is running an older version than latest."""
    if latest is None:
        latest = _latest_version
    if not latest or not device_version:
        return False
    return _parse_version(device_version) < _parse_version(latest)


async def _fetch_latest_version() -> Optional[str]:
    """Query the GitHub API for the latest release tag."""
    url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers={"Accept": "application/vnd.github+json"})
            if resp.status_code == 200:
                tag = resp.json().get("tag_name", "")
                # Tags are like "v0.6.1" — strip the leading "v"
                return tag.lstrip("v") if tag else None
            logger.warning("GitHub API returned %d", resp.status_code)
    except Exception:
        logger.debug("Failed to fetch latest version from GitHub", exc_info=True)
    return None


async def version_check_loop() -> None:
    """Background loop that checks GitHub releases periodically."""
    global _latest_version

    # Initial check after a short delay (let the app start up)
    try:
        await asyncio.sleep(10)
    except asyncio.CancelledError:
        return

    while True:
        try:
            version = await _fetch_latest_version()
        except asyncio.CancelledError:
            return
        if version:
            if _latest_version != version:
                logger.info("Latest device release: %s", version)
            _latest_version = version
        try:
            await asyncio.sleep(CHECK_INTERVAL)
        except asyncio.CancelledError:
            return
