"""Tests for the Assistant per-user daily token budget (PR 6 of 6).

Covers the standalone service layer + the router-level 429 surface:

* Default cap is honoured when nothing is configured.
* Per-user override beats the default.
* Negative cap means "unlimited".
* `check_budget` raises `BudgetExceededError` at/above the cap.
* `/message` and `/stream` both return 429 with the cap details when
  the user is over.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from cms.models.chat_message import ChatMessage
from cms.models.chat_thread import ChatThread
from cms.models.user import User


# ── Helpers ───────────────────────────────────────────────────────────


async def _create_thread(client, title: str = "") -> uuid.UUID:
    resp = await client.post("/api/chat/threads", json={"title": title})
    assert resp.status_code == 201, resp.text
    return uuid.UUID(resp.json()["id"])


async def _seed_usage(app, thread_id: uuid.UUID, tokens_in: int, tokens_out: int) -> None:
    """Persist a synthetic assistant message with the given token counts
    so the budget query has something to sum."""
    from cms.database import get_db

    factory = app.dependency_overrides[get_db]
    async for db in factory():
        db.add(
            ChatMessage(
                thread_id=thread_id,
                role="assistant",
                content="(synthetic)",
                tokens_in=tokens_in,
                tokens_out=tokens_out,
            )
        )
        await db.commit()
        break


async def _seed_old_usage(
    app, thread_id: uuid.UUID, tokens_in: int, tokens_out: int
) -> None:
    """Same as `_seed_usage` but back-dates the row by 2 days so we can
    confirm it's excluded from today's window."""
    from cms.database import get_db

    factory = app.dependency_overrides[get_db]
    async for db in factory():
        row = ChatMessage(
            thread_id=thread_id,
            role="assistant",
            content="(synthetic-old)",
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )
        db.add(row)
        await db.flush()
        row.created_at = datetime.now(timezone.utc) - timedelta(days=2)
        await db.commit()
        break


async def _admin_user(db):
    return (
        await db.execute(select(User).where(User.username == "admin"))
    ).scalar_one()


# ── Service layer ─────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestBudgetService:
    async def test_default_cap_when_unset(self, app):
        from cms.database import get_db
        from cms.services.assistant.budget import (
            DEFAULT_DAILY_TOKEN_CAP,
            get_default_cap,
        )

        factory = app.dependency_overrides[get_db]
        async for db in factory():
            cap = await get_default_cap(db)
            assert cap == DEFAULT_DAILY_TOKEN_CAP
            break

    async def test_set_and_get_default_cap(self, app):
        from cms.database import get_db
        from cms.services.assistant.budget import (
            get_default_cap,
            set_default_cap,
        )

        factory = app.dependency_overrides[get_db]
        async for db in factory():
            await set_default_cap(db, 9000)
            assert await get_default_cap(db) == 9000
            break

    async def test_user_override_beats_default(self, app):
        from cms.database import get_db
        from cms.services.assistant.budget import (
            get_user_daily_cap,
            set_default_cap,
            set_user_override,
        )

        factory = app.dependency_overrides[get_db]
        async for db in factory():
            await set_default_cap(db, 100)
            user = await _admin_user(db)
            await set_user_override(db, user.id, 5000)
            assert await get_user_daily_cap(db, user) == 5000
            break

    async def test_today_usage_sums_only_todays_rows(self, app, client):
        from cms.database import get_db
        from cms.services.assistant.budget import get_user_today_usage

        tid = await _create_thread(client)
        await _seed_usage(app, tid, 100, 200)
        await _seed_old_usage(app, tid, 999, 999)  # 2 days ago — excluded

        factory = app.dependency_overrides[get_db]
        async for db in factory():
            user = await _admin_user(db)
            used = await get_user_today_usage(db, user)
            assert used == 300
            break

    async def test_check_budget_raises_at_cap(self, app, client):
        from cms.database import get_db
        from cms.services.assistant.budget import (
            BudgetExceededError,
            check_budget,
            set_default_cap,
        )

        tid = await _create_thread(client)
        await _seed_usage(app, tid, 500, 500)

        factory = app.dependency_overrides[get_db]
        async for db in factory():
            await set_default_cap(db, 1000)
            user = await _admin_user(db)
            with pytest.raises(BudgetExceededError) as ei:
                await check_budget(db, user)
            assert ei.value.daily_cap == 1000
            assert ei.value.used == 1000
            break

    async def test_negative_cap_is_unlimited(self, app, client):
        from cms.database import get_db
        from cms.services.assistant.budget import (
            check_budget,
            set_user_override,
        )

        tid = await _create_thread(client)
        await _seed_usage(app, tid, 100_000, 100_000)

        factory = app.dependency_overrides[get_db]
        async for db in factory():
            user = await _admin_user(db)
            await set_user_override(db, user.id, -1)
            used, cap = await check_budget(db, user)
            assert used == 200_000
            assert cap == -1
            break

    async def test_clear_user_override_reverts_to_default(self, app):
        from cms.database import get_db
        from cms.services.assistant.budget import (
            clear_user_override,
            get_user_daily_cap,
            set_default_cap,
            set_user_override,
        )

        factory = app.dependency_overrides[get_db]
        async for db in factory():
            user = await _admin_user(db)
            await set_default_cap(db, 7777)
            await set_user_override(db, user.id, 100)
            assert await get_user_daily_cap(db, user) == 100
            await clear_user_override(db, user.id)
            assert await get_user_daily_cap(db, user) == 7777
            break


# ── Router-level 429 enforcement ──────────────────────────────────────


@pytest.mark.asyncio
class TestBudget429:
    async def test_message_returns_429_when_over_cap(self, app, client):
        from cms.database import get_db
        from cms.services.assistant.budget import set_default_cap

        tid = await _create_thread(client)
        await _seed_usage(app, tid, 1, 1)
        factory = app.dependency_overrides[get_db]
        async for db in factory():
            await set_default_cap(db, 1)  # forces 2/1 over
            break

        resp = await client.post(
            f"/api/chat/threads/{tid}/message", json={"content": "hi"}
        )
        assert resp.status_code == 429, resp.text
        detail = resp.json()["detail"]
        assert detail["daily_cap"] == 1
        assert detail["used"] == 2
        assert resp.headers.get("retry-after") == "3600"

    async def test_stream_returns_429_before_handshake(self, app, client):
        from cms.database import get_db
        from cms.services.assistant.budget import set_default_cap

        tid = await _create_thread(client)
        await _seed_usage(app, tid, 50, 50)
        factory = app.dependency_overrides[get_db]
        async for db in factory():
            await set_default_cap(db, 10)
            break

        resp = await client.post(
            f"/api/chat/threads/{tid}/stream", json={"content": "hi"}
        )
        assert resp.status_code == 429, resp.text
        detail = resp.json()["detail"]
        assert detail["daily_cap"] == 10
        assert detail["used"] == 100

