"""Assistant chat API — thread CRUD + feature-flag introspection (PR 2 of 6).

This PR is intentionally skeletal: just enough surface to let the
frontend land its sidebar and history view in a later PR.  No LLM,
no MCP, no SSE.  The data model is here, the gate is here, the
endpoints are here, and that's it.

Every endpoint is gated on
:func:`cms.services.assistant_flag.assistant_enabled_for` — when the
caller is not allowlisted, the API returns **404** (not 403) so that
the feature appears not to exist for users who shouldn't even know
it's coming.  Admins (``settings:write``) always pass the gate as an
escape hatch.
"""

from __future__ import annotations

import uuid
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from cms.auth import get_current_user, get_settings, require_auth
from cms.config import Settings
from cms.database import get_db
from cms.models.chat_message import ChatMessage
from cms.models.chat_thread import ChatThread
from cms.models.user import User
from cms.schemas.chat import (
    AssistantFeatureStatus,
    ChatMessageCreate,
    ChatMessageOut,
    ChatThreadCreate,
    ChatThreadOut,
)
from cms.services.assistant.agent import run_user_turn
from cms.services.assistant.llm_client import (
    AssistantUnavailableError,
    is_available as assistant_llm_available,
)
from cms.services.assistant_flag import assistant_enabled_for


router = APIRouter(prefix="/api/chat", dependencies=[Depends(require_auth)])


async def _current_user(
    request: Request,
    settings: Settings = Depends(get_settings),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Return the authenticated User.

    Mirrors the pattern used by ``cms.routers.asset_views`` so handlers
    can take ``user: User = Depends(_current_user)`` without repeating
    the request/settings/db plumbing.
    """
    return await get_current_user(request, settings, db)


async def _require_feature(user: User, db: AsyncSession) -> None:
    """Raise 404 unless the Assistant feature is enabled for ``user``.

    404 instead of 403 keeps the feature invisible to users who are not
    in the allowlist — they shouldn't be able to tell that a hidden
    chat backend exists.
    """
    if not await assistant_enabled_for(db, user):
        raise HTTPException(status_code=404, detail="Not found")


async def _get_owned_thread(
    thread_id: uuid.UUID, user: User, db: AsyncSession
) -> ChatThread:
    """Fetch ``thread_id`` or raise 404 if missing / not owned by ``user``.

    404 (not 403) on cross-user access to avoid leaking thread existence.
    """
    thread = (
        await db.execute(select(ChatThread).where(ChatThread.id == thread_id))
    ).scalar_one_or_none()
    if not thread or thread.user_id != user.id:
        raise HTTPException(status_code=404, detail="Thread not found")
    return thread


# ── Feature flag introspection ────────────────────────────────────────
# The frontend hits this on page load to decide whether to render the
# Assistant tab.  Unlike the rest of the router this endpoint never
# 404s — it always reports an enabled boolean, so the UI can branch
# without retry logic.

@router.get("/feature", response_model=AssistantFeatureStatus)
async def get_feature_status(
    user: User = Depends(_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Report whether the caller has the Assistant feature enabled."""
    return AssistantFeatureStatus(enabled=await assistant_enabled_for(db, user))


# ── Thread CRUD ───────────────────────────────────────────────────────

@router.get("/threads", response_model=List[ChatThreadOut])
async def list_threads(
    user: User = Depends(_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List the caller's threads, newest-updated first."""
    await _require_feature(user, db)
    rows = (
        await db.execute(
            select(ChatThread)
            .where(ChatThread.user_id == user.id)
            .order_by(ChatThread.updated_at.desc())
        )
    ).scalars().all()
    return [ChatThreadOut.model_validate(t) for t in rows]


@router.post("/threads", response_model=ChatThreadOut, status_code=201)
async def create_thread(
    payload: ChatThreadCreate,
    user: User = Depends(_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a new empty thread for the caller.

    ``title`` is optional; the agent loop (PR 3) will summarise the
    first user message and stamp the title automatically.  Until then
    the UI renders the first message as a placeholder title.
    """
    await _require_feature(user, db)
    thread = ChatThread(user_id=user.id, title=payload.title.strip())
    db.add(thread)
    await db.flush()
    await db.commit()
    await db.refresh(thread)
    return ChatThreadOut.model_validate(thread)


@router.delete("/threads/{thread_id}", status_code=204)
async def delete_thread(
    thread_id: uuid.UUID,
    user: User = Depends(_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Hard-delete a thread and all of its messages."""
    await _require_feature(user, db)
    thread = await _get_owned_thread(thread_id, user, db)
    await db.execute(delete(ChatThread).where(ChatThread.id == thread.id))
    await db.commit()


@router.get(
    "/threads/{thread_id}/messages", response_model=List[ChatMessageOut]
)
async def list_thread_messages(
    thread_id: uuid.UUID,
    user: User = Depends(_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return every message in the thread in chronological order.

    No pagination in Phase 1 — a single thread is bounded by the budget
    cap (PR 6) long before message counts would matter.
    """
    await _require_feature(user, db)
    thread = await _get_owned_thread(thread_id, user, db)
    rows = (
        await db.execute(
            select(ChatMessage)
            .where(ChatMessage.thread_id == thread.id)
            .order_by(ChatMessage.created_at)
        )
    ).scalars().all()
    return [ChatMessageOut.model_validate(m) for m in rows]


# ── Chat turn (PR 3a: non-streaming, no tools) ────────────────────────
# Posts a user message and runs a single LLM turn.  Returns the
# assistant message synchronously — no streaming yet (that lands in
# PR 3b alongside MCP tool calls).

@router.post(
    "/threads/{thread_id}/message",
    response_model=ChatMessageOut,
    status_code=201,
)
async def post_message(
    thread_id: uuid.UUID,
    payload: ChatMessageCreate,
    user: User = Depends(_current_user),
    settings: Settings = Depends(get_settings),
    db: AsyncSession = Depends(get_db),
):
    """Send a user prompt and return the assistant's reply.

    Returns 503 if the environment hasn't been wired up with Azure
    OpenAI yet (allowlisted user but no LLM backend).  This is distinct
    from the feature-flag 404 — the feature IS on for this user, but
    the backend is missing.
    """
    await _require_feature(user, db)
    thread = await _get_owned_thread(thread_id, user, db)

    if not assistant_llm_available(settings):
        raise HTTPException(
            status_code=503,
            detail="Assistant LLM backend is not configured in this environment.",
        )

    try:
        assistant_row = await run_user_turn(
            db=db,
            settings=settings,
            user=user,
            thread=thread,
            user_message=payload.content,
        )
    except AssistantUnavailableError as exc:
        # Defensive — assistant_llm_available() should have caught this,
        # but a race against config changes is theoretically possible.
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return ChatMessageOut.model_validate(assistant_row)
