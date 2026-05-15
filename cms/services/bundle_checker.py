"""Periodically check GitHub for the latest agora-os bundle release.

Replaces ``version_checker.py`` (which polled ``sslivins/agora`` for
agora-app debs) as part of the CMS upgrade-path migration: the
authoritative source of an OS update is now an agora-os GitHub Release
carrying a signed ``agora-bundle-*.tar.zst`` and a sidecar
``meta.json`` describing the bundle (target version, min-from version,
sha256 manifest of bundle contents, etc.).

Per cache cycle (default 30 min) this module makes two HTTPS calls:

1.  ``GET .../releases?per_page=10`` — list recent releases, filter out
    drafts, take the newest by ``published_at``.
2.  ``GET <meta.json browser_download_url>`` — pull the floor + audit
    metadata for that release.

The cached :class:`BundleInfo` is consumed by the CMS upgrade endpoint
to construct the ``os_update_dispatch`` WPS message (M3+) and by the
UI to decide whether to show an "update available" badge (M6).

Authentication: if ``GITHUB_TOKEN`` is set in the environment, requests
are made authenticated (5000 calls/hr instead of 60/hr). The CMS does
not require auth at the current poll cadence — one cycle uses ~2 calls
— but if the workspace is restart-y, auth keeps us comfortably under
the limit.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger("agora.cms.bundle_checker")

GITHUB_REPO = "sslivins/agora-os"
AGORA_OS_RELEASES_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases"
CHECK_INTERVAL = 1800  # 30 minutes

# Pattern for the bundle tarball asset on each release.  meta.json is
# matched by exact filename suffix because there's only one per release.
_BUNDLE_NAME_RE = re.compile(r"^agora-bundle-.*\.tar\.zst$")
_BUNDLE_SIG_RE = re.compile(r"^agora-bundle-.*\.tar\.zst\.minisig$")
_BUNDLE_SHA256_RE = re.compile(r"^agora-bundle-.*\.tar\.zst\.sha256$")
_BUNDLE_META_RE = re.compile(r"^agora-bundle-.*\.meta\.json$")


@dataclass(frozen=True)
class BundleInfo:
    """Snapshot of an agora-os release's OTA-relevant metadata.

    Sourced from a combination of the GitHub release object and the
    sidecar ``meta.json`` asset.  ``target_version`` has the leading
    ``v`` stripped (the producer-side ``build-bundle.sh`` already
    normalizes this in ``meta.json["version"]``, so we don't have to
    strip it here, but we tolerate a leading ``v`` defensively).
    """

    target_version: str
    release_id: str
    min_from_version: str
    bundle_url: str
    signature_url: str
    sha256_url: Optional[str]
    size_bytes: int
    created_at: str  # release.published_at, ISO-8601


# ── Module-level cache ──────────────────────────────────────────────
# Reset by check_now() and bundle_check_loop().  Tests poke these
# directly to stand up a deterministic state for is_os_update_available
# / get_latest_os_version assertions.
_latest_bundle: Optional[BundleInfo] = None
_last_success_at: Optional[datetime] = None
_last_error: Optional[str] = None


def get_latest_bundle() -> Optional[BundleInfo]:
    """Return the cached BundleInfo for the newest agora-os release, or None."""
    return _latest_bundle


def get_latest_os_version() -> Optional[str]:
    """Return the cached newest agora-os version, or None if unknown.

    This is the v-stripped semver string (e.g. ``"0.0.17-test"``).
    """
    return _latest_bundle.target_version if _latest_bundle else None


def get_status() -> dict:
    """Return checker health snapshot for debug endpoints (P1-4)."""
    return {
        "latest_version": get_latest_os_version(),
        "last_success_at": _last_success_at.isoformat() if _last_success_at else None,
        "last_error": _last_error,
    }


def _parse_version(v: str) -> tuple:
    """Parse a version string into a comparable tuple of ints.

    Handles our ``MAJOR.MINOR.PATCH[-LABEL]`` convention by splitting
    on both ``.`` and ``-`` and taking the leading run of numeric
    segments.  ``"0.0.17-test"`` → ``(0, 0, 17)`` so that
    ``parse("0.0.17-test") > parse("0.0.16-test")`` is True and
    ``parse("0.0.17-test") == parse("0.0.17")`` is True (we treat
    ``-test`` as the same release as the non-suffixed form).

    Returns the empty tuple on parse failure, which compares less than
    any populated tuple — the same fall-back behaviour as the old
    version_checker._parse_version.
    """
    if not v:
        return ()
    v = v.lstrip("v")
    out: list[int] = []
    for chunk in re.split(r"[.\-+]", v):
        if not chunk:
            break
        try:
            out.append(int(chunk))
        except ValueError:
            break
    return tuple(out)


def is_os_update_available(current_os_version: Optional[str], latest: Optional[str] = None) -> bool:
    """Return True iff the device is running an older OS version than the latest.

    Mirrors ``version_checker.is_update_available`` semantics:
    ``current_os_version`` and ``latest`` may be None (returns False),
    and comparison falls back to the cached ``get_latest_os_version()``
    when ``latest`` is omitted.

    As of M6, all callers pass ``device.os_version`` (populated by the
    M4-device register message, sslivins/agora#205, first shipped in
    agora.deb v1.11.61 and bundled into agora-os v0.0.18-test).  Devices
    on older OS bundles still report a NULL ``os_version`` and will
    short-circuit to False below — i.e. their Update button stays
    dark, which is the correct behaviour (their firmware_version namespace
    is no longer comparable to bundle target_version).
    """
    if latest is None:
        latest = get_latest_os_version()
    if not latest or not current_os_version:
        return False
    return _parse_version(current_os_version) < _parse_version(latest)


# ── GitHub fetch ────────────────────────────────────────────────────


def _gh_headers() -> dict:
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def _fetch_latest_bundle() -> Optional[BundleInfo]:
    """Query GitHub Releases for the newest agora-os bundle.

    Two GETs per call:
      1. List recent releases, pick newest non-draft by published_at.
         We include prereleases — the dev Pi rides ``-test`` tags.
      2. Fetch that release's meta.json asset for min_from_version.

    Returns None on any failure (network, missing asset, malformed
    meta.json).  Caller decides whether to keep the prior cache value
    or treat as unknown.
    """
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                AGORA_OS_RELEASES_URL,
                params={"per_page": 10},
                headers=_gh_headers(),
            )
            if resp.status_code != 200:
                logger.warning("GitHub API /releases returned %d", resp.status_code)
                return None
            releases = resp.json()

            non_draft = [r for r in releases if not r.get("draft")]
            if not non_draft:
                logger.debug("No non-draft agora-os releases found")
                return None
            non_draft.sort(key=lambda r: r.get("published_at") or "", reverse=True)
            release = non_draft[0]

            assets = release.get("assets", [])
            bundle_asset = next((a for a in assets if _BUNDLE_NAME_RE.match(a["name"])), None)
            sig_asset = next((a for a in assets if _BUNDLE_SIG_RE.match(a["name"])), None)
            sha256_asset = next((a for a in assets if _BUNDLE_SHA256_RE.match(a["name"])), None)
            meta_asset = next((a for a in assets if _BUNDLE_META_RE.match(a["name"])), None)

            if not bundle_asset or not sig_asset or not meta_asset:
                logger.warning(
                    "Release %s missing required assets (bundle=%s sig=%s meta=%s)",
                    release.get("tag_name"),
                    bool(bundle_asset),
                    bool(sig_asset),
                    bool(meta_asset),
                )
                return None

            meta_resp = await client.get(meta_asset["browser_download_url"], headers=_gh_headers())
            if meta_resp.status_code != 200:
                logger.warning(
                    "Failed to fetch meta.json for %s: HTTP %d",
                    release.get("tag_name"),
                    meta_resp.status_code,
                )
                return None
            meta = meta_resp.json()

            target_version = meta.get("version") or (release.get("tag_name") or "").lstrip("v")
            min_from_version = meta.get("min_from_version")
            if not target_version or not min_from_version:
                logger.warning(
                    "Release %s meta.json missing version/min_from_version: %r",
                    release.get("tag_name"),
                    {k: meta.get(k) for k in ("version", "min_from_version")},
                )
                return None

            return BundleInfo(
                target_version=target_version.lstrip("v"),
                release_id=str(release.get("id")),
                min_from_version=str(min_from_version),
                bundle_url=bundle_asset["browser_download_url"],
                signature_url=sig_asset["browser_download_url"],
                sha256_url=sha256_asset["browser_download_url"] if sha256_asset else None,
                size_bytes=int(bundle_asset.get("size", 0)),
                created_at=release.get("published_at") or "",
            )
    except Exception as exc:
        logger.debug("Failed to fetch latest agora-os bundle", exc_info=True)
        # Preserve last_error so the debug endpoint surfaces stale polls.
        global _last_error
        _last_error = f"{type(exc).__name__}: {exc}"
        return None


async def check_now() -> Optional[BundleInfo]:
    """Trigger an immediate poll and update the cache.

    Returns the new BundleInfo on success or the previously-cached
    value on failure (so callers can rely on ``get_latest_bundle()``
    not abruptly going None during transient GitHub flakes).
    """
    global _latest_bundle, _last_success_at, _last_error
    bundle = await _fetch_latest_bundle()
    if bundle is not None:
        _latest_bundle = bundle
        _last_success_at = datetime.now(timezone.utc)
        _last_error = None
    return _latest_bundle


async def bundle_check_loop() -> None:
    """Background loop that polls GitHub releases periodically."""
    global _latest_bundle, _last_success_at, _last_error

    try:
        await asyncio.sleep(10)
    except asyncio.CancelledError:
        return

    while True:
        try:
            bundle = await _fetch_latest_bundle()
        except asyncio.CancelledError:
            return
        if bundle is not None:
            prev_version = _latest_bundle.target_version if _latest_bundle else None
            if prev_version != bundle.target_version:
                logger.info(
                    "Latest agora-os release: %s (was %s)",
                    bundle.target_version,
                    prev_version,
                )
            _latest_bundle = bundle
            _last_success_at = datetime.now(timezone.utc)
            _last_error = None
        try:
            await asyncio.sleep(CHECK_INTERVAL)
        except asyncio.CancelledError:
            return
