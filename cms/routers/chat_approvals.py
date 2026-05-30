"""Assistant chat: write-tool approval endpoints (PR 4 of 6).

This router lives alongside :mod:`cms.routers.chat` rather than inside
it because the approval flow is its own surface — distinct lifecycle,
distinct schemas, distinct invariants.  Keeping it in a separate
module also makes the auth / feature-gate plumbing easier to test in
isolation.

Endpoints
---------

* ``GET    /api/chat/threads/{tid}/approvals`` — list pending approvals.
* ``GET    /api/chat/approvals/{aid}``         — fetch one (any state).
* ``POST   /api/chat/approvals/{aid}/approve`` — execute the tool, then
  persist a ``role=tool`` ChatMessage so the LLM can reason about it
  on the user's next turn.  Marks the approval row ``approved``.
* ``POST   /api/chat/approvals/{aid}/reject``  — synthesise a tool
  result that tells the LLM the user declined, persist it as a
  ``role=tool`` ChatMessage, mark the approval row ``rejected``.

The chat router emits an ``approval_request`` SSE event with the
approval row's ``id`` whenever the streaming agent loop hits a write
tool; the UI uses that id to call the endpoints here.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cms.auth import get_current_user, get_settings, require_auth
from cms.config import Settings
from cms.database import get_db
from cms.models.chat_message import ChatMessage
from cms.models.chat_pending_approval import (
    STATUS_APPROVED,
    STATUS_PENDING,
    STATUS_REJECTED,
    ChatPendingApproval,
)
from cms.models.chat_thread import ChatThread
from cms.models.user import User
from cms.schemas.chat import ChatApprovalDecision, ChatPendingApprovalOut
from cms.services.assistant.mcp_client import (
    AssistantMcpClient,
    McpUnavailableError,
)
from cms.services.assistant_flag import assistant_enabled_for

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/api/chat", dependencies=[Depends(require_auth)])


async def _current_user(
    request: Request,
    settings: Settings = Depends(get_settings),
    db: AsyncSession = Depends(get_db),
) -> User:
    return await get_current_user(request, settings, db)


async def _require_feature(user: User, db: AsyncSession) -> None:
    """Mirror ``cms.routers.chat._require_feature``."""
    if not await assistant_enabled_for(db, user):
        raise HTTPException(status_code=404, detail="Not found")


async def _get_owned_thread(
    thread_id: uuid.UUID, user: User, db: AsyncSession
) -> ChatThread:
    thread = (
        await db.execute(select(ChatThread).where(ChatThread.id == thread_id))
    ).scalar_one_or_none()
    if not thread or thread.user_id != user.id:
        raise HTTPException(status_code=404, detail="Thread not found")
    return thread


async def _get_owned_approval(
    approval_id: uuid.UUID, user: User, db: AsyncSession
) -> tuple[ChatPendingApproval, ChatThread]:
    """Fetch the approval row + its parent thread.

    404 (not 403) if the row doesn't exist OR the parent thread isn't
    owned by ``user`` — same leak-prevention pattern as the rest of
    the chat router.
    """
    row = (
        await db.execute(
            select(ChatPendingApproval).where(
                ChatPendingApproval.id == approval_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Approval not found")
    thread = await _get_owned_thread(row.thread_id, user, db)
    return row, thread


def _ensure_pending(row: ChatPendingApproval) -> None:
    if row.status != STATUS_PENDING:
        raise HTTPException(
            status_code=409,
            detail=f"Approval already {row.status!r}; decisions are terminal.",
        )


# ── Listing / fetch ───────────────────────────────────────────────────


@router.get(
    "/threads/{thread_id}/approvals",
    response_model=List[ChatPendingApprovalOut],
)
async def list_thread_approvals(
    thread_id: uuid.UUID,
    user: User = Depends(_current_user),
    db: AsyncSession = Depends(get_db),
    status: str | None = None,
):
    """List approval rows for ``thread_id``.

    Defaults to ``status=pending`` so the UI's "needs your attention"
    badge has a cheap query.  Pass ``status=`` (empty) to fetch every
    state for the audit timeline.
    """
    await _require_feature(user, db)
    await _get_owned_thread(thread_id, user, db)

    stmt = select(ChatPendingApproval).where(
        ChatPendingApproval.thread_id == thread_id
    )
    effective_status = STATUS_PENDING if status is None else status
    if effective_status:
        stmt = stmt.where(ChatPendingApproval.status == effective_status)
    stmt = stmt.order_by(ChatPendingApproval.created_at)
    rows = (await db.execute(stmt)).scalars().all()
    return [ChatPendingApprovalOut.model_validate(r) for r in rows]


@router.get(
    "/approvals/{approval_id}",
    response_model=ChatPendingApprovalOut,
)
async def get_approval(
    approval_id: uuid.UUID,
    user: User = Depends(_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _require_feature(user, db)
    row, _ = await _get_owned_approval(approval_id, user, db)
    return ChatPendingApprovalOut.model_validate(row)


# ── Decide ────────────────────────────────────────────────────────────


async def _upsert_tool_result(
    db: AsyncSession,
    thread_id: uuid.UUID,
    tool_call_id: str,
    content: str,
) -> None:
    """Write the tool result for ``tool_call_id`` into the thread.

    The streaming agent persists a *placeholder* ``role=tool`` row at
    the moment a write tool is intercepted so the OpenAI conversation
    history stays internally consistent (every ``tool_calls`` assistant
    turn must have a matching ``role=tool`` row).  When the human
    later approves or rejects, we UPDATE that placeholder in place
    rather than INSERTing a duplicate — otherwise the next turn's
    history would carry two rows with the same ``tool_call_id``,
    which is a wire-protocol violation OpenAI may or may not reject
    silently.

    If no placeholder exists (legacy data, or an approval created by
    the non-streaming path that didn't go through the intercept), we
    INSERT a fresh row — the upsert is idempotent on first call.
    """
    existing = (
        await db.execute(
            select(ChatMessage)
            .where(ChatMessage.thread_id == thread_id)
            .where(ChatMessage.tool_call_id == tool_call_id)
            .where(ChatMessage.role == "tool")
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.content = content
        return
    db.add(
        ChatMessage(
            thread_id=thread_id,
            role="tool",
            tool_call_id=tool_call_id,
            content=content,
        )
    )


def _result_message(
    thread_id: uuid.UUID, tool_call_id: str, content: str
) -> ChatMessage:
    """Legacy synchronous helper, kept for callers that still INSERT
    a fresh row.  Prefer :func:`_upsert_tool_result` which handles
    the placeholder-update case from the streaming approval flow."""
    return ChatMessage(
        thread_id=thread_id,
        role="tool",
        tool_call_id=tool_call_id,
        content=content,
    )


@router.post(
    "/approvals/{approval_id}/approve",
    response_model=ChatPendingApprovalOut,
)
async def approve_approval(
    approval_id: uuid.UUID,
    payload: ChatApprovalDecision | None = None,
    user: User = Depends(_current_user),
    settings: Settings = Depends(get_settings),
    db: AsyncSession = Depends(get_db),
):
    """Execute the proposed write tool via MCP and persist the result.

    On success the agent loop's next turn (i.e. when the user types
    again) sees a ``role=tool`` history row and can carry on as if the
    tool had run normally.  No in-flight stream is resumed — keeping
    "approve" decoupled from a live SSE connection avoids a whole
    class of reconnect / orphan-generator failure modes.
    """
    await _require_feature(user, db)
    row, _thread = await _get_owned_approval(approval_id, user, db)
    _ensure_pending(row)

    arguments = dict(row.tool_arguments or {})
    try:
        async with AssistantMcpClient(settings=settings, user=user) as mcp:
            # bypass_whitelist=True is the whole reason this path
            # exists — the read-only whitelist exists to defend against
            # the LLM running writes WITHOUT a human in the loop.  Here
            # the human just clicked Approve.
            result_text = await mcp.call_tool(
                row.tool_name, arguments, bypass_whitelist=True
            )
    except McpUnavailableError as exc:
        # The row stays ``pending`` so the user can retry.
        raise HTTPException(
            status_code=503,
            detail=f"Assistant tool backend unavailable: {exc}",
        ) from exc
    except Exception as exc:
        # Tool execution failed — record the failure as a tool-result
        # the LLM can reason about, but mark the row approved (the
        # user did approve; MCP just returned an error).  This matches
        # how the non-approval path handles tool errors.
        logger.exception(
            "assistant.approve.tool_failed name=%s user=%s",
            row.tool_name,
            user.id,
        )
        result_text = json.dumps({"error": f"{type(exc).__name__}: {exc}"})

    await _upsert_tool_result(db, row.thread_id, row.tool_call_id, result_text)
    row.status = STATUS_APPROVED
    row.result_content = result_text
    row.decision_note = (payload.note if payload else None) or None
    row.decided_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(row)
    logger.info(
        "assistant.approve.ok approval=%s tool=%s user=%s",
        row.id,
        row.tool_name,
        user.id,
    )
    try:
        from cms.metrics import (
            ATTR_DECISION,
            assistant_approval_decided_total,
        )
        assistant_approval_decided_total.add(1, {ATTR_DECISION: "approve"})
    except Exception:  # noqa: BLE001
        logger.debug(
            "assistant.approval_decided metric emit failed", exc_info=True
        )
    return ChatPendingApprovalOut.model_validate(row)


@router.post(
    "/approvals/{approval_id}/reject",
    response_model=ChatPendingApprovalOut,
)
async def reject_approval(
    approval_id: uuid.UUID,
    payload: ChatApprovalDecision | None = None,
    user: User = Depends(_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Decline the proposed write tool and tell the LLM the user said no.

    We still persist a ``role=tool`` row so the conversation history
    is internally consistent (every ``tool_calls`` assistant turn must
    have matching ``tool`` rows for the OpenAI API to accept the
    history on the next turn).  The content is a structured JSON blob
    so the LLM can reliably detect the rejection.
    """
    await _require_feature(user, db)
    row, _thread = await _get_owned_approval(approval_id, user, db)
    _ensure_pending(row)

    note = (payload.note if payload else None) or None
    rejection_payload: dict[str, object] = {
        "rejected": True,
        "reason": note or "User declined to approve this action.",
    }
    result_text = json.dumps(rejection_payload)

    await _upsert_tool_result(db, row.thread_id, row.tool_call_id, result_text)
    row.status = STATUS_REJECTED
    row.result_content = result_text
    row.decision_note = note
    row.decided_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(row)
    logger.info(
        "assistant.reject.ok approval=%s tool=%s user=%s",
        row.id,
        row.tool_name,
        user.id,
    )
    try:
        from cms.metrics import (
            ATTR_DECISION,
            assistant_approval_decided_total,
        )
        assistant_approval_decided_total.add(1, {ATTR_DECISION: "reject"})
    except Exception:  # noqa: BLE001
        logger.debug(
            "assistant.approval_decided metric emit failed", exc_info=True
        )
    return ChatPendingApprovalOut.model_validate(row)
