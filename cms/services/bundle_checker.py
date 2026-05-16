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

Shared state across replicas (issue agora-cms#578)
----------------------------------------------------
The "latest bundle" used to live in module-level globals
(``_latest_bundle``, ``_last_success_at``).  In a multi-replica deploy
each worker had its own copy, so a successful ``POST /api/devices/
check-updates`` only refreshed the cache of the replica it happened to
land on; the others kept their stale view until their own 30-min cron
tick fired, and the UI's ``update_available`` badge flickered on/off
as the load balancer round-robined requests.

The bundle and the last-success timestamp now live in the
``agora_os_latest_bundle`` single-row table (migration 0026).  All
readers go through :func:`get_latest_bundle` / :func:`get_latest_os_version`,
which read this row.  All writers go through :func:`set_latest_bundle`,
which UPSERTs the row.  Multiple replicas writing the same content is
fine — last-write-wins on identical payloads is idempotent.

``_last_error`` deliberately stays as a module global — it's per-replica
debug state and it's *more* useful that way (lets ops see whether one
replica's network egress is partially broken vs. all of them failing).
It's surfaced via :func:`get_status` for the debug endpoint.
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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cms.models.agora_os_latest_bundle import AgoraOsLatestBundle

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


# ── Module-level state ──────────────────────────────────────────────
# ``_last_error`` is intentionally per-replica: it's debug state for
# ``get_status()`` and exists so an operator can tell whether a single
# replica's poll is failing or whether they all are.  The actual bundle
# and last-success timestamp live in the shared ``agora_os_latest_bundle``
# row instead (see :func:`get_latest_bundle`).
_last_error: Optional[str] = None


def _bundle_from_row(row: AgoraOsLatestBundle) -> BundleInfo:
    return BundleInfo(
        target_version=row.target_version,
        release_id=row.release_id,
        min_from_version=row.min_from_version,
        bundle_url=row.bundle_url,
        signature_url=row.signature_url,
        sha256_url=row.sha256_url,
        size_bytes=row.size_bytes,
        created_at=row.created_at,
    )


async def get_latest_bundle(db: AsyncSession) -> Optional[BundleInfo]:
    """Return the latest known BundleInfo (shared across replicas), or None.

    Reads from the ``agora_os_latest_bundle`` single-row table.  Returns
    ``None`` only when no poll has ever succeeded for this CMS deployment
    (cold start before the first ``bundle_check_loop`` iteration writes
    the row).
    """
    row = await db.scalar(
        select(AgoraOsLatestBundle).where(AgoraOsLatestBundle.id == 1)
    )
    if row is None:
        return None
    return _bundle_from_row(row)


async def get_latest_os_version(db: AsyncSession) -> Optional[str]:
    """Return the latest known agora-os version, or None if no successful poll yet.

    This is the v-stripped semver string (e.g. ``"0.0.17-test"``).
    """
    bundle = await get_latest_bundle(db)
    return bundle.target_version if bundle else None


async def get_status(db: AsyncSession) -> dict:
    """Return checker health snapshot for debug endpoints (P1-4).

    ``last_error`` is per-replica (this process's last failed poll, if
    any).  ``latest_version`` and ``last_success_at`` are read from the
    shared DB row so all replicas report the same value for these.
    """
    row = await db.scalar(
        select(AgoraOsLatestBundle).where(AgoraOsLatestBundle.id == 1)
    )
    return {
        "latest_version": row.target_version if row else None,
        "last_success_at": row.last_success_at.isoformat() if row else None,
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


def is_os_update_available(
    current_os_version: Optional[str],
    latest: Optional[str],
) -> bool:
    """Return True iff the device is running an older OS version than ``latest``.

    Pure function — does NOT consult any cache or DB.  Callers must pass
    ``latest`` explicitly (typically obtained once per HTTP request via
    ``await get_latest_os_version(db)``).  Issue #578: this is deliberate
    — the old implicit-fallback behaviour was the source of the per-replica
    cache drift.

    Either argument may be ``None`` (returns ``False``).  As of M6, all
    callers pass ``device.os_version`` (populated by the M4-device register
    message, sslivins/agora#205).  Devices on older OS bundles still report
    a NULL ``os_version`` and short-circuit to ``False`` below — i.e. their
    Update button stays dark, which is the correct behaviour (their
    firmware_version namespace is no longer comparable to bundle
    target_version).
    """
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
    meta.json).  Side-effects: updates the module-local ``_last_error``
    (per-replica debug state surfaced via :func:`get_status`).  Caller
    decides whether to keep the prior DB row or treat as unknown.
    """
    global _last_error
    try:
        # follow_redirects=True is REQUIRED: GitHub release-asset
        # browser_download_url endpoints always 302 to the actual blob
        # storage URL on objects.githubusercontent.com.  Without this
        # flag every meta.json fetch returns HTTP 302 and check_now()
        # silently returns None -- the UI then shows "latest version:
        # unknown" with no clue why.
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(
                AGORA_OS_RELEASES_URL,
                params={"per_page": 10},
                headers=_gh_headers(),
            )
            if resp.status_code != 200:
                msg = f"GitHub API /releases returned {resp.status_code}"
                logger.warning(msg)
                _last_error = msg
                return None
            releases = resp.json()

            non_draft = [r for r in releases if not r.get("draft")]
            if not non_draft:
                msg = "No non-draft agora-os releases found"
                logger.debug(msg)
                _last_error = msg
                return None
            non_draft.sort(key=lambda r: r.get("published_at") or "", reverse=True)
            release = non_draft[0]

            assets = release.get("assets", [])
            bundle_asset = next((a for a in assets if _BUNDLE_NAME_RE.match(a["name"])), None)
            sig_asset = next((a for a in assets if _BUNDLE_SIG_RE.match(a["name"])), None)
            sha256_asset = next((a for a in assets if _BUNDLE_SHA256_RE.match(a["name"])), None)
            meta_asset = next((a for a in assets if _BUNDLE_META_RE.match(a["name"])), None)

            if not bundle_asset or not sig_asset or not meta_asset:
                msg = (
                    f"Release {release.get('tag_name')!r} missing required assets "
                    f"(bundle={bool(bundle_asset)} sig={bool(sig_asset)} meta={bool(meta_asset)})"
                )
                logger.warning(msg)
                _last_error = msg
                return None

            meta_resp = await client.get(meta_asset["browser_download_url"], headers=_gh_headers())
            if meta_resp.status_code != 200:
                msg = (
                    f"Failed to fetch meta.json for {release.get('tag_name')!r}: "
                    f"HTTP {meta_resp.status_code}"
                )
                logger.warning(msg)
                _last_error = msg
                return None
            meta = meta_resp.json()

            target_version = meta.get("version") or (release.get("tag_name") or "").lstrip("v")
            min_from_version = meta.get("min_from_version")
            if not target_version or not min_from_version:
                meta_subset = {k: meta.get(k) for k in ("version", "min_from_version")}
                msg = (
                    f"Release {release.get('tag_name')!r} meta.json missing version/"
                    f"min_from_version: {meta_subset!r}"
                )
                logger.warning(msg)
                _last_error = msg
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
        _last_error = f"{type(exc).__name__}: {exc}"
        return None


# ── DB persistence ──────────────────────────────────────────────────


async def set_latest_bundle(db: AsyncSession, bundle: BundleInfo) -> None:
    """UPSERT the shared single-row ``agora_os_latest_bundle`` table.

    Caller is responsible for committing the session.  Stamps
    ``last_success_at`` to ``datetime.now(UTC)``.  Public so tests
    (which previously poked ``_latest_bundle`` directly) can seed
    state in a multi-replica-correct way.
    """
    now = datetime.now(timezone.utc)
    row = await db.scalar(
        select(AgoraOsLatestBundle).where(AgoraOsLatestBundle.id == 1)
    )
    if row is None:
        db.add(
            AgoraOsLatestBundle(
                id=1,
                target_version=bundle.target_version,
                release_id=bundle.release_id,
                min_from_version=bundle.min_from_version,
                bundle_url=bundle.bundle_url,
                signature_url=bundle.signature_url,
                sha256_url=bundle.sha256_url,
                size_bytes=bundle.size_bytes,
                created_at=bundle.created_at,
                last_success_at=now,
            )
        )
    else:
        row.target_version = bundle.target_version
        row.release_id = bundle.release_id
        row.min_from_version = bundle.min_from_version
        row.bundle_url = bundle.bundle_url
        row.signature_url = bundle.signature_url
        row.sha256_url = bundle.sha256_url
        row.size_bytes = bundle.size_bytes
        row.created_at = bundle.created_at
        row.last_success_at = now


async def check_now(db: AsyncSession) -> Optional[BundleInfo]:
    """Trigger an immediate poll and update the shared DB row.

    Returns the new BundleInfo on success or the previously-stored
    value on failure (so callers can rely on a successful ``check_now``
    not abruptly going None during transient GitHub flakes).
    """
    global _last_error
    bundle = await _fetch_latest_bundle()
    if bundle is not None:
        await set_latest_bundle(db, bundle)
        await db.commit()
        _last_error = None
        return bundle
    # Fetch failed; surface whatever the DB has (may still be None on
    # cold-start before any successful poll has ever landed).
    return await get_latest_bundle(db)


async def bundle_check_loop() -> None:
    """Background loop that polls GitHub releases periodically.

    Runs replicated across all CMS replicas (writes are idempotent on
    identical payloads, so concurrent writers converge to the same
    row content).
    """
    global _last_error

    # Lazy import — avoids a hard cms.database dep at module import time
    # so the test layer can import the module before init_db() runs.
    from cms.database import get_session_factory

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
            sf = get_session_factory()
            if sf is None:
                # Session factory not initialised (very early startup or
                # tests that import this module without init_db()).
                # Best-effort: skip this iteration, retry next tick.
                logger.debug("bundle_check_loop: session factory not ready; skipping write")
            else:
                try:
                    async with sf() as db:
                        prev = await db.scalar(
                            select(AgoraOsLatestBundle.target_version).where(
                                AgoraOsLatestBundle.id == 1
                            )
                        )
                        if prev != bundle.target_version:
                            logger.info(
                                "Latest agora-os release: %s (was %s)",
                                bundle.target_version,
                                prev,
                            )
                        await set_latest_bundle(db, bundle)
                        await db.commit()
                    _last_error = None
                except Exception:
                    # Don't crash the loop on a transient DB error -- the
                    # in-process _last_error is left alone so a stuck DB
                    # surfaces on the debug endpoint as the GH-side error,
                    # but we log it for completeness.
                    logger.warning(
                        "bundle_check_loop: failed to persist latest bundle",
                        exc_info=True,
                    )
        try:
            await asyncio.sleep(CHECK_INTERVAL)
        except asyncio.CancelledError:
            return

