"""Pydantic schemas for the Assistant chat API."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


class ChatThreadOut(BaseModel):
    """A chat thread as returned by the API."""

    model_config = {"from_attributes": True}

    id: uuid.UUID
    title: str
    created_at: datetime
    updated_at: datetime


class ChatThreadCreate(BaseModel):
    """Request body for ``POST /api/chat/threads``."""

    title: str = Field(default="", max_length=200)


class ChatMessageCreate(BaseModel):
    """Request body for ``POST /api/chat/threads/{id}/message``.

    Single field — the user's prompt.  The agent loop is responsible
    for persisting both the user turn AND the assistant turn; callers
    just send the prompt and get back the assistant's reply.
    """

    content: str = Field(min_length=1, max_length=10_000)


class ChatMessageOut(BaseModel):
    """A single message in a thread.

    ``tool_calls`` and ``tool_call_id`` are surfaced on the wire because
    the frontend needs them to render the tool-call timeline (and, in
    PR 4, the approval cards).  ``tokens_in`` / ``tokens_out`` are NOT
    surfaced; the budget badge reads from ``/api/chat/budget`` instead.
    """

    model_config = {"from_attributes": True}

    id: uuid.UUID
    thread_id: uuid.UUID
    role: str
    content: str
    tool_calls: Optional[list[dict[str, Any]]] = None
    tool_call_id: Optional[str] = None
    created_at: datetime


class AssistantFeatureStatus(BaseModel):
    """Response body for ``GET /api/chat/feature``.

    Lets the frontend decide whether to render the Assistant tab without
    needing to probe a 404 from another endpoint.
    """

    enabled: bool


# ── PR 4: write-tool approval flow ────────────────────────────────────


class ChatPendingApprovalOut(BaseModel):
    """A pending write-tool approval as returned by the API.

    Surfaces the proposed tool name + arguments so the UI can render an
    Approve / Reject card with the same detail level it would use for a
    completed tool call.  ``status`` lets the UI hide already-decided
    rows from the pending list without a separate filter param.
    """

    model_config = {"from_attributes": True}

    id: uuid.UUID
    thread_id: uuid.UUID
    proposed_by_message_id: Optional[uuid.UUID] = None
    tool_name: str
    tool_call_id: str
    tool_arguments: dict[str, Any]
    status: str
    result_content: Optional[str] = None
    decision_note: Optional[str] = None
    created_at: datetime
    decided_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None


class ChatApprovalDecision(BaseModel):
    """Request body for ``POST /api/chat/approvals/{id}/{approve|reject}``.

    ``note`` is an optional free-form audit string.  We don't enforce
    it on either path — UIs may collect a reason on reject and leave
    it blank on approve.
    """

    note: Optional[str] = Field(default=None, max_length=500)
