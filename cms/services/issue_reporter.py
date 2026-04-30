"""GitHub issue reporter — file an issue from the topbar "Report" modal.

Wraps the GitHub Issues REST API.  Requires a fine-grained PAT with
``Issues: Read and Write`` scoped to the configured repo.  The token is
read from settings (``github_issues_token``); when absent the feature
is disabled at the UI layer (button hidden) and any direct API call
returns 503.
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx

from cms.auth import get_settings

logger = logging.getLogger("agora.cms.issue_reporter")

_TIMEOUT = 15.0


class IssueReporterError(Exception):
    """Raised when issue creation fails."""

    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


def is_enabled() -> bool:
    """Return True if a GitHub token is configured."""
    return bool(get_settings().github_issues_token)


async def create_issue(
    title: str,
    body: str,
    *,
    labels: Optional[list[str]] = None,
) -> dict:
    """Create a GitHub issue.

    Returns the parsed JSON response from the GitHub API on success.
    Raises :class:`IssueReporterError` with an appropriate ``status_code``
    when the token is missing, the API rejects the request, or the
    network call fails.
    """
    settings = get_settings()
    token = settings.github_issues_token
    if not token:
        raise IssueReporterError("GitHub issues integration is not configured", status_code=503)

    repo = settings.github_issues_repo
    if "/" not in repo:
        raise IssueReporterError(f"Invalid github_issues_repo: {repo!r}", status_code=500)

    url = f"https://api.github.com/repos/{repo}/issues"
    payload: dict = {"title": title, "body": body}
    if labels:
        payload["labels"] = labels

    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(url, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        logger.warning("GitHub issue create failed: network error: %s", exc)
        raise IssueReporterError("Failed to reach GitHub", status_code=502) from exc

    if resp.status_code == 201:
        data = resp.json()
        logger.info(
            "Created GitHub issue #%s in %s: %s",
            data.get("number"), repo, data.get("html_url"),
        )
        return data

    # Surface the GitHub error message when available
    detail = ""
    try:
        detail = resp.json().get("message", "") or ""
    except Exception:
        pass
    logger.warning(
        "GitHub issue create returned %d: %s", resp.status_code, detail or resp.text[:200]
    )
    if resp.status_code in (401, 403):
        raise IssueReporterError(
            "GitHub rejected the request — check the configured PAT scopes",
            status_code=502,
        )
    if resp.status_code == 404:
        raise IssueReporterError(
            f"Repo {repo!r} not found or the PAT does not have access",
            status_code=502,
        )
    raise IssueReporterError(
        f"GitHub returned {resp.status_code}: {detail or 'unknown error'}",
        status_code=502,
    )
