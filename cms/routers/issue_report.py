"""Router for the topbar "Report an issue" feature.

POST /api/issues/report — create a GitHub issue with the user-supplied
title and description plus server-merged context (current user, app
version, timestamp).  Available to any authenticated user.  Returns
503 when no GitHub token is configured.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from cms import __version__ as cms_version
from cms.auth import get_current_user, get_settings
from cms.config import Settings
from cms.models.user import User
from cms.services import issue_reporter

router = APIRouter(prefix="/api/issues")

_MAX_TITLE_LEN = 200
_MAX_BODY_LEN = 8000
_MAX_FIELD_LEN = 500


class ReportIssueRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=_MAX_TITLE_LEN)
    description: str = Field(..., min_length=1, max_length=_MAX_BODY_LEN)
    page_url: Optional[str] = Field(default=None, max_length=_MAX_FIELD_LEN)
    user_agent: Optional[str] = Field(default=None, max_length=_MAX_FIELD_LEN)


class ReportIssueResponse(BaseModel):
    ok: bool
    number: int
    html_url: str


def _build_body(req: ReportIssueRequest, user: User) -> str:
    """Compose the GitHub issue body from the user description plus context."""
    description = req.description.strip()
    page_url = (req.page_url or "").strip() or "(unknown)"
    user_agent = (req.user_agent or "").strip() or "(unknown)"
    submitter = user.display_name or user.email or "(unknown)"
    submitter_email = user.email or "(unknown)"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    return (
        f"{description}\n\n"
        "---\n"
        "**Reported from Agora CMS**\n\n"
        f"- **User:** {submitter} ({submitter_email})\n"
        f"- **CMS version:** {cms_version}\n"
        f"- **Page:** {page_url}\n"
        f"- **User agent:** {user_agent}\n"
        f"- **Timestamp:** {now}\n"
    )


@router.get("/report/config")
async def report_config(user: User = Depends(get_current_user)) -> dict:
    """Tell the UI whether the report-issue feature is enabled.

    The button is rendered server-side via a template flag, but this
    endpoint is useful for diagnostics and lets the modal guard the
    submit if the token is removed mid-session.
    """
    return {"enabled": issue_reporter.is_enabled()}


@router.post("/report", response_model=ReportIssueResponse)
async def report_issue(
    payload: ReportIssueRequest,
    request: Request,
    user: User = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> ReportIssueResponse:
    if not issue_reporter.is_enabled():
        raise HTTPException(status_code=503, detail="Issue reporting is not configured")

    # Prefer a client-supplied page URL (with hash/path) but fall back to
    # the Referer header so the issue body has *something* useful even
    # when the JS forgets to send it.
    if not payload.page_url:
        ref = request.headers.get("referer")
        if ref:
            payload.page_url = ref[:_MAX_FIELD_LEN]
    if not payload.user_agent:
        ua = request.headers.get("user-agent")
        if ua:
            payload.user_agent = ua[:_MAX_FIELD_LEN]

    body = _build_body(payload, user)
    label = settings.github_issues_label
    labels = [label] if label else None

    try:
        result = await issue_reporter.create_issue(
            payload.title.strip(), body, labels=labels
        )
    except issue_reporter.IssueReporterError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    return ReportIssueResponse(
        ok=True,
        number=int(result.get("number", 0)),
        html_url=str(result.get("html_url", "")),
    )
