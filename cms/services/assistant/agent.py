"""Assistant agent: orchestrates a single chat turn.

PR 3b flow (non-streaming, MCP tool-calling loop):

1. Caller posts a user message to ``POST /api/chat/threads/{id}/message``.
2. :func:`run_user_turn` is invoked:
   a. Persists the user's :class:`ChatMessage`.
   b. Opens an :class:`AssistantMcpClient` (SSE handshake + MCP
      ``initialize``) using the CMS service key + ``X-On-Behalf-Of:
      <user.id>`` header so per-user RBAC is preserved.
   c. Loads the thread's existing messages (chronological) and builds
      the OpenAI-format conversation: system prompt + history + the
      new user turn.
   d. Loops up to ``assistant_max_tool_iterations`` times:
        i. Call the LLM with the current ``messages`` + read-only
           tool list.
       ii. If the model returns no ``tool_calls``: persist a final
           ``assistant`` row with the text and break out of the loop.
      iii. Otherwise persist an ``assistant`` row that carries the
           ``tool_calls`` (text content may be empty), then execute
           each tool call against MCP, persist a ``tool`` row per
           result, append everything to ``messages``, and loop again.
   e. If the loop hits its cap without a clean answer, persist a final
      ``assistant`` row with a "(stopped: max tool iterations reached)"
      message so the user sees something.
   f. Auto-title untitled threads from the first user prompt.
   g. Bump ``thread.updated_at`` so it sorts to the top of the
      sidebar.

The agent owns the DB transaction boundary — the router doesn't have
to think about it.  Token-budget enforcement and approval gating land
in later PRs; this module's signature is shaped so adding them won't
require a router change.
"""
from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cms.config import Settings
from cms.models.chat_message import ChatMessage
from cms.models.chat_pending_approval import (
    STATUS_PENDING,
    ChatPendingApproval,
)
from cms.models.chat_thread import ChatThread
from cms.models.user import User
from cms.services.assistant.llm_client import (
    CompletionResult,
    LLMClient,
)
from cms.services.assistant.mcp_client import (
    AssistantMcpClient,
    McpUnavailableError,
    READ_ONLY_TOOLS,
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

    Preserves ``tool_calls`` on assistant turns and ``tool_call_id`` on
    tool turns so an interrupted multi-step turn (e.g. server restart
    between tool result and final answer) replays correctly on the
    next call.
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


def _parse_tool_arguments(raw: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Return ``(args, error)``.  ``error`` is non-None on bad JSON."""
    if raw is None or raw == "":
        return {}, None
    if isinstance(raw, dict):
        return raw, None
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        return None, f"invalid_json_arguments: {exc}"
    if not isinstance(parsed, dict):
        return None, "tool arguments must be a JSON object"
    return parsed, None


async def _execute_tool_call(
    *,
    mcp: AssistantMcpClient,
    tool_call: dict[str, Any],
) -> str:
    """Run a single OpenAI ``tool_call`` against MCP, returning a string.

    Never raises — failures are encoded as JSON ``{"error": ...}`` so
    the LLM can see the failure and reason about it on the next turn.
    """
    fn = tool_call.get("function", {}) or {}
    name = fn.get("name", "")
    args, err = _parse_tool_arguments(fn.get("arguments"))
    if err is not None:
        return json.dumps({"error": "bad_arguments", "message": err})
    if name not in READ_ONLY_TOOLS:
        return json.dumps(
            {
                "error": "tool_not_allowed",
                "message": (
                    f"Tool '{name}' is not in the read-only whitelist. "
                    "Write/mutating tools require an approval flow that "
                    "isn't built yet (PR 4)."
                ),
            }
        )
    try:
        return await mcp.call_tool(name, args or {})
    except PermissionError as exc:
        # Should be unreachable (whitelist check above) but defensive.
        return json.dumps({"error": "tool_not_allowed", "message": str(exc)})
    except Exception as exc:  # noqa: BLE001 — surface as tool result
        logger.exception("assistant.tool.exec_failed tool=%s", name)
        return json.dumps(
            {
                "error": "tool_execution_failed",
                "message": f"{type(exc).__name__}: {exc}",
            }
        )


async def run_user_turn(
    *,
    db: AsyncSession,
    settings: Settings,
    user: User,
    thread: ChatThread,
    user_message: str,
    llm_client: LLMClient | None = None,
    mcp_client: AssistantMcpClient | None = None,
) -> ChatMessage:
    """Run one user → assistant turn against ``thread``.

    Returns the persisted assistant :class:`ChatMessage` that the caller
    should serialize back to the HTTP client (the final text reply, not
    an intermediate tool-call turn).  Caller-supplied ``llm_client`` /
    ``mcp_client`` are used if provided (tests inject fakes); otherwise
    fresh ones are constructed and closed at the end of the call.
    """
    user_message = user_message.strip()
    if not user_message:
        raise ValueError("user_message must not be empty")

    # 0. Daily-cap check.  Done BEFORE persisting the user row so a
    #    user who is at the cap doesn't accumulate empty turns; the
    #    router converts BudgetExceededError into a 429 with the
    #    cap details and the user sees a friendly message.
    from cms.services.assistant.budget import check_budget
    await check_budget(db, user)

    # 1. Persist the user turn first so it's visible in history even if
    #    the LLM call later fails (the frontend's optimistic render
    #    will reconcile to this row on the next poll).
    user_row = ChatMessage(
        thread_id=thread.id, role="user", content=user_message
    )
    db.add(user_row)
    await db.flush()

    # 2. Reload the full history (now including the row we just added)
    #    in chronological order.  Single round-trip; threads in Phase 1
    #    are bounded by the token budget cap (PR 6) so no pagination
    #    needed.
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

    # 4. Open clients.  Both raise *Unavailable / construction errors
    #    BEFORE we mutate the DB further, so the router can return 503
    #    cleanly with only the user turn persisted (which is correct —
    #    the user typed it, it should appear in history).
    owns_llm = llm_client is None
    llm = llm_client if llm_client is not None else LLMClient(settings)
    owns_mcp = mcp_client is None
    mcp = mcp_client if mcp_client is not None else AssistantMcpClient(
        settings=settings, user=user
    )
    if owns_mcp:
        await mcp.__aenter__()

    final_assistant_row: ChatMessage | None = None
    try:
        tools = await mcp.list_openai_tools()
        max_iters = max(1, int(settings.assistant_max_tool_iterations))

        for iteration in range(max_iters):
            result: CompletionResult = await llm.complete(
                openai_messages, tools=tools or None
            )

            if not result.tool_calls:
                # Final answer turn — persist text + token accounting.
                final_assistant_row = ChatMessage(
                    thread_id=thread.id,
                    role="assistant",
                    content=result.content,
                    tokens_in=result.tokens_in,
                    tokens_out=result.tokens_out,
                )
                db.add(final_assistant_row)
                openai_messages.append(
                    {"role": "assistant", "content": result.content}
                )
                break

            # Intermediate tool-call turn — persist with tool_calls.
            tc_row = ChatMessage(
                thread_id=thread.id,
                role="assistant",
                content=result.content or "",
                tool_calls=result.tool_calls,
                tokens_in=result.tokens_in,
                tokens_out=result.tokens_out,
            )
            db.add(tc_row)
            openai_messages.append(
                {
                    "role": "assistant",
                    "content": result.content or "",
                    "tool_calls": result.tool_calls,
                }
            )

            # Execute each tool call and persist its result.
            for tool_call in result.tool_calls:
                tool_content = await _execute_tool_call(
                    mcp=mcp, tool_call=tool_call
                )
                tool_row = ChatMessage(
                    thread_id=thread.id,
                    role="tool",
                    content=tool_content,
                    tool_call_id=tool_call.get("id"),
                )
                db.add(tool_row)
                openai_messages.append(
                    {
                        "role": "tool",
                        "content": tool_content,
                        "tool_call_id": tool_call.get("id"),
                    }
                )
            await db.flush()
        else:
            # Hit the iteration cap without a tool_calls-free response.
            logger.warning(
                "assistant.turn.max_iters_hit thread=%s user=%s cap=%d",
                thread.id,
                user.id,
                max_iters,
            )
            final_assistant_row = ChatMessage(
                thread_id=thread.id,
                role="assistant",
                content=(
                    "(stopped: max tool iterations reached. The assistant "
                    "kept requesting more tools without producing a final "
                    "answer. Try rephrasing the request.)"
                ),
            )
            db.add(final_assistant_row)
    finally:
        if owns_mcp:
            await mcp.__aexit__(None, None, None)
        if owns_llm:
            await llm.aclose()

    # 5. Auto-title untitled threads from the first user prompt.
    user_turn_count = sum(1 for r in rows if r.role == "user")
    if user_turn_count == 1 and not thread.title.strip():
        thread.title = _auto_title(user_message)

    # 6. Bump `updated_at` so this thread sorts to the top of the
    #    sidebar.  Set it explicitly because SQLAlchemy only fires an
    #    UPDATE when at least one mapped attribute is actually dirty.
    thread.updated_at = datetime.now(timezone.utc)
    await db.flush()
    await db.commit()
    assert final_assistant_row is not None  # always set by either branch
    await db.refresh(final_assistant_row)
    logger.info(
        "assistant.turn.complete thread=%s user=%s in=%d out=%d",
        thread.id,
        user.id,
        final_assistant_row.tokens_in,
        final_assistant_row.tokens_out,
    )
    try:
        from cms.metrics import (
            ATTR_STREAMING,
            assistant_message_sent_total,
        )
        assistant_message_sent_total.add(1, {ATTR_STREAMING: "false"})
    except Exception:  # noqa: BLE001 - telemetry must never break the turn
        logger.debug("assistant.message_sent metric emit failed", exc_info=True)
    return final_assistant_row


# ── Streaming variant (PR 3c) ─────────────────────────────────────────


def _assemble_tool_calls(
    by_index: dict[int, dict[str, Any]],
) -> list[dict[str, Any]] | None:
    """Convert accumulated streaming tool-call fragments to OpenAI shape.

    Returns ``None`` when no fragments were seen.  Empty-name fragments
    are dropped (defensive against partial Azure responses).
    """
    if not by_index:
        return None
    out: list[dict[str, Any]] = []
    for idx in sorted(by_index.keys()):
        tc = by_index[idx]
        if not tc.get("name"):
            continue
        out.append(
            {
                "id": tc.get("id") or f"call_{idx}",
                "type": "function",
                "function": {
                    "name": tc["name"],
                    "arguments": tc.get("arguments", ""),
                },
            }
        )
    return out or None


async def run_user_turn_streaming(
    *,
    db: AsyncSession,
    settings: Settings,
    user: User,
    thread: ChatThread,
    user_message: str,
    llm_client: LLMClient | None = None,
    mcp_client: AssistantMcpClient | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Streaming counterpart of :func:`run_user_turn`.

    Yields event dicts the SSE endpoint serialises to the client:

    * ``{"type": "token", "text": str}`` — assistant text chunk.
    * ``{"type": "tool_call", "id", "name", "arguments"}`` — tool invocation
      starting (after the LLM finished requesting it).
    * ``{"type": "tool_result", "id", "name", "content"}`` — tool result.
    * ``{"type": "done", "message_id", "tokens_in", "tokens_out"}`` —
      final answer turn complete.
    * ``{"type": "error", "message"}`` — fatal error; the generator
      raises after yielding (caller turns it into the SSE error frame
      and disconnects).

    Persistence rules mirror :func:`run_user_turn`: every assistant /
    tool turn is written to the DB as it happens so an interrupted
    stream still leaves a coherent thread.
    """
    user_message = user_message.strip()
    if not user_message:
        raise ValueError("user_message must not be empty")

    # 0. Daily-cap check (same as run_user_turn).  Raised before any
    #    DB mutation so the router can surface 429 BEFORE the SSE
    #    handshake — the user never sees a half-open stream that
    #    immediately errors out.
    from cms.services.assistant.budget import check_budget
    await check_budget(db, user)

    # 1. Persist the user turn first.
    user_row = ChatMessage(
        thread_id=thread.id, role="user", content=user_message
    )
    db.add(user_row)
    await db.flush()

    rows = (
        await db.execute(
            select(ChatMessage)
            .where(ChatMessage.thread_id == thread.id)
            .order_by(ChatMessage.created_at, ChatMessage.id)
        )
    ).scalars().all()

    openai_messages: list[dict[str, Any]] = [
        {"role": "system", "content": build_system_prompt(user)},
    ]
    openai_messages.extend(_history_to_openai_messages(list(rows)))

    owns_llm = llm_client is None
    llm = llm_client if llm_client is not None else LLMClient(settings)
    owns_mcp = mcp_client is None
    mcp = mcp_client if mcp_client is not None else AssistantMcpClient(
        settings=settings, user=user
    )
    if owns_mcp:
        await mcp.__aenter__()

    final_row: ChatMessage | None = None
    try:
        tools = await mcp.list_openai_tools()
        max_iters = max(1, int(settings.assistant_max_tool_iterations))

        for iteration in range(max_iters):
            full_content: list[str] = []
            tc_by_idx: dict[int, dict[str, Any]] = {}
            tokens_in = 0
            tokens_out = 0
            pending_approvals: list[dict[str, Any]] = []

            async for delta in llm.stream(
                openai_messages, tools=tools or None
            ):
                dtype = delta.get("type")
                if dtype == "content":
                    full_content.append(delta["text"])
                    yield {"type": "token", "text": delta["text"]}
                elif dtype == "tool_call_delta":
                    idx = delta.get("index", 0)
                    slot = tc_by_idx.setdefault(
                        idx, {"id": None, "name": "", "arguments": ""}
                    )
                    if delta.get("id"):
                        slot["id"] = delta["id"]
                    if delta.get("name"):
                        slot["name"] = (slot["name"] or "") + delta["name"]
                    if delta.get("arguments_delta"):
                        slot["arguments"] += delta["arguments_delta"]
                elif dtype == "finish":
                    tokens_in = delta.get("tokens_in", 0) or tokens_in
                    tokens_out = delta.get("tokens_out", 0) or tokens_out

            content = "".join(full_content)
            tool_calls = _assemble_tool_calls(tc_by_idx)

            if not tool_calls:
                final_row = ChatMessage(
                    thread_id=thread.id,
                    role="assistant",
                    content=content,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                )
                db.add(final_row)
                openai_messages.append(
                    {"role": "assistant", "content": content}
                )
                break

            tc_row = ChatMessage(
                thread_id=thread.id,
                role="assistant",
                content=content,
                tool_calls=tool_calls,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
            )
            db.add(tc_row)
            await db.flush()
            openai_messages.append(
                {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": tool_calls,
                }
            )

            for tool_call in tool_calls:
                fn = tool_call.get("function", {}) or {}
                tc_name = fn.get("name") or ""
                tc_id = tool_call.get("id") or ""
                yield {
                    "type": "tool_call",
                    "id": tc_id,
                    "name": tc_name,
                    "arguments": fn.get("arguments", ""),
                }

                # PR 4: write-tool approval intercept.  Any tool name
                # NOT in the read-only whitelist becomes an approval
                # request instead of an MCP call.  We still persist a
                # placeholder ``role=tool`` row so the conversation
                # history stays internally consistent (OpenAI rejects
                # any assistant-with-tool_calls turn that isn't followed
                # by matching tool rows on the next API call).  The
                # approve / reject endpoint OVERWRITES this row's
                # content with the real result once the user decides.
                if tc_name and tc_name not in READ_ONLY_TOOLS:
                    parsed_args, parse_err = _parse_tool_arguments(
                        fn.get("arguments")
                    )
                    approval_args = (
                        parsed_args
                        if parse_err is None
                        else {"_unparsed_arguments": fn.get("arguments")}
                    )
                    approval = ChatPendingApproval(
                        thread_id=thread.id,
                        proposed_by_message_id=tc_row.id,
                        tool_name=tc_name,
                        tool_call_id=tc_id,
                        tool_arguments=approval_args,
                        status=STATUS_PENDING,
                    )
                    db.add(approval)
                    await db.flush()

                    placeholder = json.dumps(
                        {
                            "status": "awaiting_approval",
                            "approval_id": str(approval.id),
                            "tool": tc_name,
                        }
                    )
                    tool_row = ChatMessage(
                        thread_id=thread.id,
                        role="tool",
                        content=placeholder,
                        tool_call_id=tc_id,
                    )
                    db.add(tool_row)
                    await db.flush()
                    openai_messages.append(
                        {
                            "role": "tool",
                            "content": placeholder,
                            "tool_call_id": tc_id,
                        }
                    )
                    pending_approvals.append(
                        {
                            "id": str(approval.id),
                            "tool": tc_name,
                            "arguments": approval_args,
                            "tool_call_id": tc_id,
                        }
                    )
                    yield {
                        "type": "approval_request",
                        "approval_id": str(approval.id),
                        "tool_call_id": tc_id,
                        "name": tc_name,
                        "arguments": approval_args,
                    }
                    continue

                tool_content = await _execute_tool_call(
                    mcp=mcp, tool_call=tool_call
                )
                tool_row = ChatMessage(
                    thread_id=thread.id,
                    role="tool",
                    content=tool_content,
                    tool_call_id=tc_id,
                )
                db.add(tool_row)
                await db.flush()
                openai_messages.append(
                    {
                        "role": "tool",
                        "content": tool_content,
                        "tool_call_id": tc_id,
                    }
                )
                yield {
                    "type": "tool_result",
                    "id": tc_id,
                    "name": tc_name,
                    "content": tool_content,
                }

            # PR 4: if any tool call in this batch was deferred to the
            # approval queue, stop the agent loop here.  The user has
            # to decide before any more LLM iterations make sense; the
            # conversation resumes when the user sends their next
            # message (which sees the placeholder tool rows + the
            # decided approval state via the approve/reject endpoint).
            if pending_approvals:
                final_row = ChatMessage(
                    thread_id=thread.id,
                    role="assistant",
                    content=(
                        "(awaiting your approval before continuing — "
                        f"{len(pending_approvals)} action"
                        f"{'s' if len(pending_approvals) != 1 else ''} "
                        "queued)"
                    ),
                )
                db.add(final_row)
                break
        else:
            logger.warning(
                "assistant.stream.max_iters_hit thread=%s user=%s cap=%d",
                thread.id,
                user.id,
                max_iters,
            )
            final_row = ChatMessage(
                thread_id=thread.id,
                role="assistant",
                content=(
                    "(stopped: max tool iterations reached. The assistant "
                    "kept requesting more tools without producing a final "
                    "answer. Try rephrasing the request.)"
                ),
            )
            db.add(final_row)
    finally:
        if owns_mcp:
            await mcp.__aexit__(None, None, None)
        if owns_llm:
            await llm.aclose()

    # Auto-title + updated_at + commit.
    user_turn_count = sum(1 for r in rows if r.role == "user")
    if user_turn_count == 1 and not thread.title.strip():
        thread.title = _auto_title(user_message)
    thread.updated_at = datetime.now(timezone.utc)
    await db.flush()
    await db.commit()
    assert final_row is not None
    await db.refresh(final_row)
    logger.info(
        "assistant.stream.complete thread=%s user=%s in=%d out=%d",
        thread.id,
        user.id,
        final_row.tokens_in,
        final_row.tokens_out,
    )
    try:
        from cms.metrics import (
            ATTR_STREAMING,
            assistant_message_sent_total,
        )
        assistant_message_sent_total.add(1, {ATTR_STREAMING: "true"})
    except Exception:  # noqa: BLE001 - telemetry must never break the turn
        logger.debug("assistant.message_sent metric emit failed", exc_info=True)
    yield {
        "type": "done",
        "message_id": str(final_row.id),
        "tokens_in": final_row.tokens_in,
        "tokens_out": final_row.tokens_out,
    }



