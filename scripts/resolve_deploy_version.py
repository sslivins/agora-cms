"""Resolve and pre-flight a Goodwill deploy version.

Reads the requested version from the environment variable INPUT_VERSION:

* If INPUT_VERSION is empty or ``auto`` (case-insensitive), discover the
  highest semver tag that exists in **all** of the three GHCR repos
  agora-cms, agora-cms-mcp, and agora-worker, and use that.
* Otherwise, use INPUT_VERSION verbatim.

In either case, HEAD the manifest for each of the three image refs in
GHCR. If any are missing, fail with the most recent co-versioned tags
as a hint, so the operator (human or agent) sees the right answer next
to the error message.

The resolved version is written to ``$GITHUB_OUTPUT`` as
``resolved_version=<x.y.z>`` so downstream steps can consume it
without re-running this script. Designed to fail fast in CI well
before Bicep gets handed bad image refs.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from typing import Iterable

ORG = "sslivins"
REPOS = ("agora-cms", "agora-cms-mcp", "agora-worker")
SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")

# Accept common manifest media types so GHCR returns 200 on HEAD instead of
# a 415 when negotiating content type for OCI vs. Docker manifests.
MANIFEST_ACCEPT = ", ".join(
    [
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
    ]
)

_TOKENS: dict[str, str] = {}


def _token(repo: str) -> str:
    if repo not in _TOKENS:
        url = f"https://ghcr.io/token?scope=repository:{ORG}/{repo}:pull"
        with urllib.request.urlopen(url, timeout=15) as resp:
            _TOKENS[repo] = json.loads(resp.read())["token"]
    return _TOKENS[repo]


def fetch_tags(repo: str) -> list[str]:
    """Return every tag in ``repo``, following GHCR's Link-header pagination.

    The default tags/list response is alphabetical-ish and capped at 100
    entries — both of those bit me when I tried to eyeball "the latest
    tag" from a single page. Always paginate, then semver-sort
    afterward.
    """

    tok = _token(repo)
    tags: list[str] = []
    url: str | None = f"https://ghcr.io/v2/{ORG}/{repo}/tags/list?n=1000"
    while url:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read())
            tags.extend(body.get("tags") or [])
            link = resp.headers.get("Link", "") or ""
        m = re.search(r'<([^>]+)>;\s*rel="next"', link)
        url = f"https://ghcr.io{m.group(1)}" if m else None
    return tags


def semver_only(tags: Iterable[str]) -> list[tuple[int, int, int, str]]:
    """Filter ``tags`` down to ``X.Y.Z`` semver and return (major, minor, patch, tag)."""

    out: list[tuple[int, int, int, str]] = []
    for t in tags:
        m = SEMVER_RE.match(t)
        if m:
            out.append((int(m.group(1)), int(m.group(2)), int(m.group(3)), t))
    return out


def manifest_exists(repo: str, version: str) -> bool:
    tok = _token(repo)
    url = f"https://ghcr.io/v2/{ORG}/{repo}/manifests/{version}"
    req = urllib.request.Request(
        url,
        method="HEAD",
        headers={"Authorization": f"Bearer {tok}", "Accept": MANIFEST_ACCEPT},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 404):
            return False
        raise


def co_versioned_tags() -> list[tuple[int, int, int, str]]:
    """Return semver tags present in **all** three repos, ascending."""

    per_repo = {repo: {t for *_, t in semver_only(fetch_tags(repo))} for repo in REPOS}
    common = set.intersection(*per_repo.values()) if per_repo else set()
    return sorted(semver_only(common))


def _emit_output(name: str, value: str) -> None:
    out = os.environ.get("GITHUB_OUTPUT")
    if not out:
        # Not running under GH Actions — print so a human invoker can see it.
        print(f"{name}={value}")
        return
    with open(out, "a", encoding="utf-8") as fh:
        fh.write(f"{name}={value}\n")


def main() -> int:
    requested = (os.environ.get("INPUT_VERSION") or "").strip()
    auto = requested.lower() in ("", "auto")

    if auto:
        print(f"::group::Resolving latest co-versioned tag across {', '.join(REPOS)}")
        common = co_versioned_tags()
        if not common:
            print("::error::No co-versioned semver tag found across all three repos")
            return 1
        resolved = common[-1][3]
        print(f"  Intersection size: {len(common)} co-versioned semver tags")
        print(f"  Resolved version : {resolved}")
        print("::endgroup::")
    else:
        resolved = requested
        print(f"Using pinned version: {resolved}")

    print("::group::Verifying GHCR manifests for resolved version")
    missing: list[str] = []
    for repo in REPOS:
        ref = f"ghcr.io/{ORG}/{repo}:{resolved}"
        if manifest_exists(repo, resolved):
            print(f"  OK   {ref}")
        else:
            print(f"  MISS {ref}")
            missing.append(repo)
    print("::endgroup::")

    if missing:
        print(
            "::error::version "
            f"{resolved} is missing from: {', '.join(missing)}. "
            "All three images must be present at the same tag — "
            "check the most recent successful 'Publish & Deploy' run on main, "
            "or rerun this workflow with version=auto."
        )
        print("::group::Most recent 10 co-versioned tags (any of these would deploy)")
        try:
            for *_, t in reversed(co_versioned_tags()[-10:]):
                print(f"  {t}")
        except Exception as e:  # pragma: no cover - best-effort hint
            print(f"  (failed to compute hint: {e})")
        print("::endgroup::")
        return 1

    _emit_output("resolved_version", resolved)
    print(f"resolved_version={resolved}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
