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

import json
import logging
import uuid
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from cms.auth import get_current_user, get_settings, require_auth
from cms.config import Settings
from cms.database import get_db
from cms.models.chat_message import ChatMessage
from cms.models.chat_thread import ChatThread
from cms.models.user import User
from cms.permissions import ASSETS_WRITE, has_permission
from cms.schemas.chat import (
    AssistantFeatureStatus,
    AssistantUsageOut,
    ChatMessageCreate,
    ChatMessageOut,
    ChatThreadCreate,
    ChatThreadOut,
)
from cms.services.assistant.agent import run_user_turn, run_user_turn_streaming
from cms.services.assistant.budget import (
    BudgetExceededError,
    check_budget,
    get_user_daily_cap,
    get_user_today_usage_split,
)
from cms.services.assistant.llm_client import (
    AssistantUnavailableError,
    is_available as assistant_llm_available,
)
from cms.services.assistant.mcp_client import (
    McpUnavailableError,
    MODE_COMPOSED_EDITOR,
)
from cms.services.assistant.pricing import estimate_usd, model_for_deployment
from cms.services.assistant_flag import assistant_enabled_for
from cms.services.audit_service import audit_log

logger = logging.getLogger(__name__)


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


async def _verify_composed_editor_thread(
    thread: ChatThread,
    user: User,
    request: Request,
    db: AsyncSession,
) -> None:
    """Re-validate a ``composed_editor`` thread at turn start.

    No-op for general threads. For an editor-bound thread this defends
    against state drift between the moment the editor opened the thread
    and the moment a message is sent:

    * orphaned thread (no bound asset) → 409 — never run the agent with a
      composed-editor prompt but no slide to scope it to;
    * the caller lost ``ASSETS_WRITE`` since the thread was created → 403;
    * the bound slide was deleted, or is no longer visible to the caller
      → 404 / 403, surfaced by ``_load_composed_for_write``.

    Without this, a stale editor thread could let the assistant attempt
    draft writes against a slide the user can no longer edit or see.
    """
    if thread.mode != MODE_COMPOSED_EDITOR:
        return
    if thread.composed_asset_id is None:
        raise HTTPException(
            status_code=409, detail="Editor thread is not bound to a slide"
        )
    if user.role is None or not has_permission(
        user.role.permissions, ASSETS_WRITE
    ):
        raise HTTPException(
            status_code=403, detail=f"Missing permission: {ASSETS_WRITE}"
        )
    # Existence + visibility (raises 404 / 403). Lazy import avoids a
    # router import cycle (composed imports nothing from chat).
    from cms.routers.composed import _load_composed_for_write  # noqa: WPS433

    await _load_composed_for_write(thread.composed_asset_id, request, db)


async def _emit_assistant_audit(
    *,
    db: AsyncSession,
    settings: Settings,
    user: User,
    thread: ChatThread,
    tokens_in: int,
    tokens_out: int,
    message_id: str | None,
    streaming: bool,
    request: Request | None = None,
) -> None:
    """Record one ``assistant.message`` audit-log entry for a chat turn.

    Wrapped in a broad try/except: audit emission must never break the
    user's turn.  Cost estimation uses the same pricing helper as the
    `/api/chat/usage` endpoint so the audit row matches what the user
    sees in the budget UI.
    """
    try:
        deployment = settings.azure_openai_deployment or ""
        model_override = getattr(settings, "azure_openai_model", "") or ""
        model = model_for_deployment(deployment, model_override=model_override)
        cost_usd = estimate_usd(
            deployment=deployment,
            tokens_in=int(tokens_in or 0),
            tokens_out=int(tokens_out or 0),
            model_override=model_override,
        )
        details: dict = {
            "model": model,
            "deployment": deployment,
            "tokens_in": int(tokens_in or 0),
            "tokens_out": int(tokens_out or 0),
            "streaming": streaming,
            "thread_title": thread.title or "",
        }
        if message_id:
            details["message_id"] = str(message_id)
        if isinstance(cost_usd, (int, float)) and cost_usd > 0:
            details["est_cost_usd"] = round(float(cost_usd), 6)

        await audit_log(
            db,
            user=user,
            action="assistant.message",
            resource_type="chat_thread",
            resource_id=str(thread.id),
            details=details,
            request=request,
        )
        await db.commit()
    except Exception:  # noqa: BLE001 — telemetry must never break the turn
        logger.exception("assistant.audit.emit_failed")


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


@router.get("/usage", response_model=AssistantUsageOut)
async def get_assistant_usage(
    user: User = Depends(_current_user),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """Return the caller's current-day token usage + cap + USD estimate.

    Gated on the same feature flag as the rest of ``/api/chat/*`` so
    non-allowlisted users get a 404 (consistent with the rule that the
    feature shouldn't even appear to exist for them).

    The USD figure is an **estimate** based on the configured AOAI
    deployment's matched model row in
    :data:`cms.services.assistant.pricing.PRICE_TABLE_USD_PER_M_TOKENS`.
    Actual Azure billing can differ (EA discounts, cached-input rates,
    etc.) — the UI labels it as "estimated" for that reason.
    """
    await _require_feature(user, db)
    tokens_in, tokens_out = await get_user_today_usage_split(db, user)
    cap = await get_user_daily_cap(db, user)
    deployment = settings.azure_openai_deployment or ""
    model_override = settings.azure_openai_model or ""
    usd = estimate_usd(
        deployment=deployment,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        model_override=model_override,
    )
    return AssistantUsageOut(
        used_tokens=tokens_in + tokens_out,
        used_tokens_in=tokens_in,
        used_tokens_out=tokens_out,
        cap_tokens=cap,
        unlimited=cap < 0,
        used_usd_estimate=usd,
        model=model_for_deployment(deployment, model_override=model_override),
    )


# ── Thread CRUD ───────────────────────────────────────────────────────

@router.get("/threads", response_model=List[ChatThreadOut])
async def list_threads(
    user: User = Depends(_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List the caller's general threads, newest-updated first.

    Composed-editor threads (``mode == "composed_editor"``) are excluded:
    they're asset-scoped ephemera owned by the slide editor's embedded
    chat panel, not standalone conversations, so they must never appear
    in the general assistant sidebar.
    """
    await _require_feature(user, db)
    rows = (
        await db.execute(
            select(ChatThread)
            .where(
                ChatThread.user_id == user.id,
                ChatThread.mode != MODE_COMPOSED_EDITOR,
            )
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
    request: Request,
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
    await _verify_composed_editor_thread(thread, user, request, db)

    try:
        await check_budget(db, user)
    except BudgetExceededError as exc:
        raise HTTPException(
            status_code=429,
            detail={
                "message": "Daily token cap reached.",
                "daily_cap": exc.daily_cap,
                "used": exc.used,
            },
            headers={"Retry-After": "3600"},
        ) from exc

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
    except BudgetExceededError as exc:
        raise HTTPException(
            status_code=429,
            detail={
                "message": "Daily token cap reached.",
                "daily_cap": exc.daily_cap,
                "used": exc.used,
            },
            headers={"Retry-After": "3600"},
        ) from exc
    except AssistantUnavailableError as exc:
        # Defensive — assistant_llm_available() should have caught this,
        # but a race against config changes is theoretically possible.
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except McpUnavailableError as exc:
        # The agent needs MCP for tool grounding; if the MCP backend is
        # down we refuse rather than reply with an ungrounded answer.
        raise HTTPException(
            status_code=503,
            detail=f"Assistant tool backend unavailable: {exc}",
        ) from exc

    await _emit_assistant_audit(
        db=db,
        settings=settings,
        user=user,
        thread=thread,
        tokens_in=getattr(assistant_row, "tokens_in", 0) or 0,
        tokens_out=getattr(assistant_row, "tokens_out", 0) or 0,
        message_id=str(assistant_row.id) if getattr(assistant_row, "id", None) else None,
        streaming=False,
        request=request,
    )

    return ChatMessageOut.model_validate(assistant_row)


# ── Streaming chat turn (PR 3c) ──────────────────────────────────────
# Same agent loop as POST /message above, but the response is an SSE
# stream so the UI can render tokens / tool-call progress as they
# happen instead of waiting for the whole turn.


def _format_sse_error(message: str) -> dict[str, str]:
    """sse-starlette dict-shape for a terminal error frame."""
    return {"event": "error", "data": json.dumps({"message": message})}


async def _stream_agent_events(
    *,
    db: AsyncSession,
    settings: Settings,
    user: User,
    thread: ChatThread,
    content: str,
    request: Request | None = None,
):
    """Adapt :func:`run_user_turn_streaming` events into sse-starlette
    ``{event, data}`` dicts.

    Catches the two known *Unavailable* errors and emits a final
    ``error`` event instead of letting them propagate — the SSE
    connection is already open at this point so an HTTP 503 wouldn't
    actually reach the client.
    """
    done_evt: dict | None = None
    try:
        async for evt in run_user_turn_streaming(
            db=db,
            settings=settings,
            user=user,
            thread=thread,
            user_message=content,
        ):
            if evt.get("type") == "done":
                done_evt = evt
            yield {"event": evt["type"], "data": json.dumps(evt)}
    except AssistantUnavailableError as exc:
        logger.warning("assistant.stream.llm_unavailable: %s", exc)
        yield _format_sse_error(f"Assistant LLM unavailable: {exc}")
    except McpUnavailableError as exc:
        logger.warning("assistant.stream.mcp_unavailable: %s", exc)
        yield _format_sse_error(f"Assistant tool backend unavailable: {exc}")
    except Exception as exc:  # noqa: BLE001 — last-resort error frame
        logger.exception("assistant.stream.unexpected_error")
        yield _format_sse_error(f"{type(exc).__name__}: {exc}")
    finally:
        if done_evt is not None:
            await _emit_assistant_audit(
                db=db,
                settings=settings,
                user=user,
                thread=thread,
                tokens_in=done_evt.get("tokens_in") or 0,
                tokens_out=done_evt.get("tokens_out") or 0,
                message_id=done_evt.get("message_id"),
                streaming=True,
                request=request,
            )


@router.post("/threads/{thread_id}/stream")
async def post_message_stream(
    thread_id: uuid.UUID,
    payload: ChatMessageCreate,
    request: Request,
    user: User = Depends(_current_user),
    settings: Settings = Depends(get_settings),
    db: AsyncSession = Depends(get_db),
):
    """Send a user prompt and stream the assistant's reply as SSE.

    The response is an ``EventSourceResponse`` with these event types
    (each ``data`` is JSON):

    * ``token`` — assistant text chunk.
    * ``tool_call`` — model is about to invoke a tool.
    * ``tool_result`` — tool finished, result attached.
    * ``done`` — turn complete (carries final ``message_id``).
    * ``error`` — fatal error; the stream terminates after this frame.

    LLM-unavailable / MCP-unavailable are surfaced as ``error`` frames
    once the SSE handshake has succeeded.  Pre-stream failures (feature
    flag, thread ownership, LLM config missing) still return HTTP
    4xx/5xx the normal way before any event is sent.
    """
    await _require_feature(user, db)
    thread = await _get_owned_thread(thread_id, user, db)
    await _verify_composed_editor_thread(thread, user, request, db)

    # Budget pre-check.  Runs BEFORE EventSourceResponse so a user
    # over their cap gets a clean 429 instead of a half-open SSE
    # stream that immediately emits an error frame.  The agent re-
    # checks internally as a belt-and-braces safety net.
    try:
        await check_budget(db, user)
    except BudgetExceededError as exc:
        raise HTTPException(
            status_code=429,
            detail={
                "message": "Daily token cap reached.",
                "daily_cap": exc.daily_cap,
                "used": exc.used,
            },
            headers={"Retry-After": "3600"},
        ) from exc

    if not assistant_llm_available(settings):
        raise HTTPException(
            status_code=503,
            detail="Assistant LLM backend is not configured in this environment.",
        )

    return EventSourceResponse(
        _stream_agent_events(
            db=db,
            settings=settings,
            user=user,
            thread=thread,
            content=payload.content,
            request=request,
        ),
        # ``ping`` keeps intermediate proxies (Container Apps front-door,
        # browsers) from closing the connection during long LLM waits.
        ping=15,
    )
