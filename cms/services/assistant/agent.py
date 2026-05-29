"""Assistant agent: orchestrates a single chat turn.

PR 3a flow (non-streaming, no tools):

1. Caller posts a user message to ``POST /api/chat/threads/{id}/message``.
2. :func:`run_user_turn` is invoked:
   a. Persists the user's :class:`ChatMessage`.
   b. Loads the thread's existing messages (chronological).
   c. Builds the OpenAI-format conversation: system prompt + history +
      the new user turn.
   d. Calls :meth:`LLMClient.complete`.
   e. Persists the assistant :class:`ChatMessage` with the response and
      token usage.
   f. Auto-titles the thread if this was the first user turn and the
      thread title is empty.
   g. Bumps the thread's ``updated_at`` so it sorts to the top of the
      sidebar.
3. The router serialises the assistant message and returns it.

The agent owns the DB transaction boundary — the router doesn't have
to think about it.  Token-budget enforcement and approval gating land
in later PRs; this module's signature is shaped so that adding them
won't require a router change.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cms.config import Settings
from cms.models.chat_message import ChatMessage
from cms.models.chat_thread import ChatThread
from cms.models.user import User
from cms.services.assistant.llm_client import (
    CompletionResult,
    LLMClient,
)
from cms.services.assistant.prompts import build_system_prompt

logger = logging.getLogger(__name__)

# Max characters used for the auto-generated thread title.  The DB
# column is 200 chars (ChatThread.title); 80 is a comfortable UI cap.
_AUTO_TITLE_MAX = 80


def _auto_title(user_message: str) -> str:
    """Return a single-line truncated version of ``user_message``."""
    flat = " ".join(user_message.split())
    if len(flat) <= _AUTO_TITLE_MAX:
        return flat
    return flat[: _AUTO_TITLE_MAX - 1].rstrip() + "\u2026"  # ellipsis


def _history_to_openai_messages(
    history: list[ChatMessage],
) -> list[dict[str, Any]]:
    """Convert persisted :class:`ChatMessage` rows to OpenAI format.

    PR 3a only emits ``user`` / ``assistant`` turns — ``tool`` /
    ``system`` rows from later PRs are preserved transparently so the
    same helper can be used after PR 3b lands.
    """
    out: list[dict[str, Any]] = []
    for row in history:
        msg: dict[str, Any] = {"role": row.role, "content": row.content}
        if row.tool_calls:
            msg["tool_calls"] = row.tool_calls
        if row.tool_call_id:
            msg["tool_call_id"] = row.tool_call_id
        out.append(msg)
    return out


async def run_user_turn(
    *,
    db: AsyncSession,
    settings: Settings,
    user: User,
    thread: ChatThread,
    user_message: str,
    llm_client: LLMClient | None = None,
) -> ChatMessage:
    """Run one user → assistant turn against ``thread``.

    Returns the persisted assistant :class:`ChatMessage`.  Caller-supplied
    ``llm_client`` is used if provided (tests inject a mock); otherwise
    a fresh client is constructed and closed at the end of the call.
    """
    user_message = user_message.strip()
    if not user_message:
        raise ValueError("user_message must not be empty")

    # 1. Persist the user turn first so it's visible in history even if
    #    the LLM call later fails (the frontend's optimistic render
    #    will reconcile to this row on the next poll).
    user_row = ChatMessage(
        thread_id=thread.id, role="user", content=user_message
    )
    db.add(user_row)
    await db.flush()

    # 2. Reload the full history (now including the row we just added)
    #    in chronological order.  Single round-trip; threads in PR 3a
    #    are bounded by user typing speed so no pagination needed.
    rows = (
        await db.execute(
            select(ChatMessage)
            .where(ChatMessage.thread_id == thread.id)
            .order_by(ChatMessage.created_at, ChatMessage.id)
        )
    ).scalars().all()

    # 3. Build the OpenAI-format payload.
    openai_messages: list[dict[str, Any]] = [
        {"role": "system", "content": build_system_prompt(user)},
    ]
    openai_messages.extend(_history_to_openai_messages(list(rows)))

    # 4. Call the LLM, owning the client lifecycle if the caller didn't
    #    inject one.
    owns_client = llm_client is None
    client = llm_client if llm_client is not None else LLMClient(settings)
    try:
        result: CompletionResult = await client.complete(openai_messages)
    finally:
        if owns_client:
            await client.aclose()

    # 5. Persist the assistant turn with token accounting.
    assistant_row = ChatMessage(
        thread_id=thread.id,
        role="assistant",
        content=result.content,
        tokens_in=result.tokens_in,
        tokens_out=result.tokens_out,
    )
    db.add(assistant_row)

    # 6. Auto-title untitled threads from the first user prompt.  We
    #    check that the only persisted user row is this one rather than
    #    relying on `not thread.title`, because the user may have
    #    cleared the title in the UI.
    user_turn_count = sum(1 for r in rows if r.role == "user")
    if user_turn_count == 1 and not thread.title.strip():
        thread.title = _auto_title(user_message)

    # 7. Bump `updated_at` so this thread sorts to the top of the
    #    sidebar.  We set it explicitly rather than relying on
    #    `onupdate=` because SQLAlchemy only fires an UPDATE when at
    #    least one mapped attribute is actually dirty.
    thread.updated_at = datetime.now(timezone.utc)
    await db.flush()
    await db.commit()
    await db.refresh(assistant_row)
    logger.info(
        "assistant.turn.complete thread=%s user=%s in=%d out=%d",
        thread.id,
        user.id,
        result.tokens_in,
        result.tokens_out,
    )
    return assistant_row
