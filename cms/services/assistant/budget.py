"""Per-user daily token budget for the Assistant.

Tracks LLM token usage by *summing* ``ChatMessage.tokens_in`` +
``tokens_out`` across the user's threads since the start of the
current UTC day.  No separate counter table is required because the
authoritative per-turn token counts are already persisted on each
assistant message (recorded from the OpenAI completion ``usage`` reply).

Configuration uses the existing ``cms_settings`` key/value store:

* ``assistant_daily_token_cap`` — integer, global default in tokens
  per UTC day per user.  Missing or unparseable → ``DEFAULT_DAILY_TOKEN_CAP``.
* ``assistant_daily_token_cap_overrides`` — JSON object mapping
  ``{user_id_str: int}`` for per-user overrides.  A user listed here
  uses their override; everyone else uses the global default.  A
  ``0`` cap means "blocked, even though allowlisted" (useful for
  temporarily revoking access without rewriting the allowlist).
  A negative cap is treated as **unlimited** (escape hatch for the
  admin running diagnostics; logged at INFO when invoked).

Enforcement is invoked from :func:`agent.run_user_turn` and
:func:`agent.run_user_turn_streaming` *before* any LLM call is made.
A :class:`BudgetExceededError` carries the cap and current usage so
the router can build a useful 429 response.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, time, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from cms.auth import get_setting, set_setting
from cms.models.chat_message import ChatMessage
from cms.models.chat_thread import ChatThread
from cms.models.user import User


logger = logging.getLogger(__name__)


# Default cap when nothing is configured.  50k tokens/day at the
# AOAI gpt-4o-mini blended rate (~$0.15/M in + $0.60/M out) is well
# under $0.05/day per user — safe Phase-1 ceiling that lets us see
# real usage before any admin has to touch a setting.
DEFAULT_DAILY_TOKEN_CAP: int = 50_000

BUDGET_CAP_KEY = "assistant_daily_token_cap"
BUDGET_OVERRIDES_KEY = "assistant_daily_token_cap_overrides"


class BudgetExceededError(Exception):
    """Raised when a user has hit their daily token cap for the day.

    Attributes:
        daily_cap: The cap that was in effect for this user.
        used:      Tokens already consumed today before this turn.
    """

    def __init__(self, *, daily_cap: int, used: int) -> None:
        self.daily_cap = daily_cap
        self.used = used
        super().__init__(
            f"Daily token cap reached ({used}/{daily_cap})."
        )


# ── Settings accessors ────────────────────────────────────────────────


async def get_default_cap(db: AsyncSession) -> int:
    """Return the configured global cap, falling back to the default."""
    raw = await get_setting(db, BUDGET_CAP_KEY)
    if raw is None or raw == "":
        return DEFAULT_DAILY_TOKEN_CAP
    try:
        return int(raw)
    except (TypeError, ValueError):
        logger.warning(
            "assistant_daily_token_cap=%r is not an int; falling back to default",
            raw,
        )
        return DEFAULT_DAILY_TOKEN_CAP


async def set_default_cap(db: AsyncSession, cap: int) -> None:
    """Persist the global default cap (admin-only)."""
    await set_setting(db, BUDGET_CAP_KEY, str(int(cap)))


async def _load_overrides(db: AsyncSession) -> dict[uuid.UUID, int]:
    raw = await get_setting(db, BUDGET_OVERRIDES_KEY)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning(
            "%s contains invalid JSON; treating as empty", BUDGET_OVERRIDES_KEY
        )
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[uuid.UUID, int] = {}
    for k, v in data.items():
        try:
            out[uuid.UUID(str(k))] = int(v)
        except (TypeError, ValueError):
            logger.warning(
                "skipping override entry %r=%r (invalid uuid or int)", k, v
            )
    return out


async def get_overrides(db: AsyncSession) -> dict[uuid.UUID, int]:
    """Return the per-user override map (admin UI uses this directly)."""
    return await _load_overrides(db)


async def set_user_override(
    db: AsyncSession, user_id: uuid.UUID, cap: int
) -> None:
    """Set an override for a single user (does not touch others)."""
    overrides = await _load_overrides(db)
    overrides[user_id] = int(cap)
    serialised = {str(uid): int(c) for uid, c in overrides.items()}
    await set_setting(db, BUDGET_OVERRIDES_KEY, json.dumps(serialised))


async def clear_user_override(db: AsyncSession, user_id: uuid.UUID) -> None:
    """Remove an override; the user reverts to the global default."""
    overrides = await _load_overrides(db)
    if overrides.pop(user_id, None) is None:
        return
    serialised = {str(uid): int(c) for uid, c in overrides.items()}
    await set_setting(db, BUDGET_OVERRIDES_KEY, json.dumps(serialised))


# ── Lookups ───────────────────────────────────────────────────────────


async def get_user_daily_cap(db: AsyncSession, user: User) -> int:
    """Return the cap that applies to ``user`` today."""
    overrides = await _load_overrides(db)
    if user.id in overrides:
        return overrides[user.id]
    return await get_default_cap(db)


def _utc_day_start(now: datetime | None = None) -> datetime:
    """Return midnight UTC of the current (or provided) instant."""
    now = now or datetime.now(timezone.utc)
    return datetime.combine(now.date(), time.min, tzinfo=timezone.utc)


async def get_user_today_usage(
    db: AsyncSession, user: User, *, now: datetime | None = None
) -> int:
    """Sum ``tokens_in + tokens_out`` for ``user``'s assistant turns today.

    Threads are scoped by ``ChatThread.user_id`` so we don't double-count
    if a thread is later transferred / inherited.  System and tool rows
    have ``tokens_in/out`` = 0 by construction so they contribute
    nothing; the implementation just adds the whole column.
    """
    day_start = _utc_day_start(now)
    stmt = (
        select(
            func.coalesce(func.sum(ChatMessage.tokens_in), 0)
            + func.coalesce(func.sum(ChatMessage.tokens_out), 0)
        )
        .select_from(ChatMessage)
        .join(ChatThread, ChatThread.id == ChatMessage.thread_id)
        .where(ChatThread.user_id == user.id)
        .where(ChatMessage.created_at >= day_start)
    )
    total = (await db.execute(stmt)).scalar()
    return int(total or 0)


async def check_budget(
    db: AsyncSession, user: User, *, now: datetime | None = None
) -> tuple[int, int]:
    """Raise :class:`BudgetExceededError` if the user is at/over their cap.

    Returns ``(used, cap)`` on success so the caller can attach the
    pair to a log line / telemetry event.

    A negative cap is treated as unlimited (admin escape hatch);
    we log INFO so the audit trail still records the use of it.
    """
    cap = await get_user_daily_cap(db, user)
    used = await get_user_today_usage(db, user, now=now)
    if cap < 0:
        logger.info(
            "assistant.budget.unlimited user=%s used=%d", user.id, used
        )
        return used, cap
    if used >= cap:
        raise BudgetExceededError(daily_cap=cap, used=used)
    return used, cap
