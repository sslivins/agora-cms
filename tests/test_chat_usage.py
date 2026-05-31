"""Tests for the Assistant per-user usage display.

Covers two layers:

* The pure pricing helper (:mod:`cms.services.assistant.pricing`) —
  deployment-name resolution, USD math, fallback behaviour, and the
  defensive clamp for corrupt token counts.
* The ``GET /api/chat/usage`` endpoint — surfaces tokens + USD for the
  caller's UTC day, gated on the Assistant feature flag.
"""

from __future__ import annotations

import uuid

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
    """Persist a synthetic assistant message so the usage query has
    something to sum."""
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


# ── Pricing unit tests ────────────────────────────────────────────────


class TestPricingResolver:
    def test_exact_match_gpt_4o_mini(self):
        from cms.services.assistant.pricing import _resolve_model

        key, (in_rate, out_rate) = _resolve_model("gpt-4o-mini")
        assert key == "gpt-4o-mini"
        assert (in_rate, out_rate) == (0.15, 0.60)

    def test_longest_substring_wins(self):
        """A deployment named ``cms-gpt-4o-mini-2024-07-18`` must
        resolve to ``gpt-4o-mini``, not the shorter ``gpt-4o``."""
        from cms.services.assistant.pricing import _resolve_model

        key, _ = _resolve_model("cms-gpt-4o-mini-2024-07-18")
        assert key == "gpt-4o-mini"

    def test_unknown_deployment_uses_fallback(self):
        from cms.services.assistant.pricing import (
            FALLBACK_RATES_USD_PER_M_TOKENS,
            _resolve_model,
        )

        key, rates = _resolve_model("totally-custom-name")
        assert key == "unknown"
        assert rates == FALLBACK_RATES_USD_PER_M_TOKENS

    def test_empty_deployment_uses_fallback(self):
        from cms.services.assistant.pricing import (
            FALLBACK_RATES_USD_PER_M_TOKENS,
            _resolve_model,
        )

        key, rates = _resolve_model("")
        assert key == "unknown"
        assert rates == FALLBACK_RATES_USD_PER_M_TOKENS

    def test_case_insensitive_match(self):
        from cms.services.assistant.pricing import _resolve_model

        key, _ = _resolve_model("CMS-GPT-4O-MINI")
        assert key == "gpt-4o-mini"


class TestEstimateUsd:
    def test_math_matches_table(self):
        from cms.services.assistant.pricing import estimate_usd

        # gpt-4o: 2.50 / 10.00 per million.
        # 1M input + 1M output → 2.50 + 10.00 = 12.50
        cost = estimate_usd(
            deployment="gpt-4o", tokens_in=1_000_000, tokens_out=1_000_000
        )
        assert cost == pytest.approx(12.50)

    def test_negative_token_counts_clamped(self):
        from cms.services.assistant.pricing import estimate_usd

        assert (
            estimate_usd(deployment="gpt-4o", tokens_in=-5, tokens_out=-5)
            == 0.0
        )

    def test_zero_tokens_returns_zero(self):
        from cms.services.assistant.pricing import estimate_usd

        assert estimate_usd(deployment="gpt-4o", tokens_in=0, tokens_out=0) == 0.0

    def test_model_for_deployment_passthrough(self):
        from cms.services.assistant.pricing import model_for_deployment

        assert model_for_deployment("cms-gpt-4o-mini") == "gpt-4o-mini"
        assert model_for_deployment("weird-deployment") == "unknown"
        assert model_for_deployment("") == "unknown"


class TestModelOverride:
    """The ``model_override`` arg is the fix for deployments whose
    *name* doesn't embed the model (e.g. our bicep names the AOAI
    deployment ``chat``, so the substring matcher would return
    ``"unknown"`` and the USD estimate would always be 0)."""

    def test_override_wins_over_unknown_deployment(self):
        from cms.services.assistant.pricing import _resolve_model

        key, rates = _resolve_model("chat", model_override="gpt-4o")
        assert key == "gpt-4o"
        # gpt-4o rates: 2.50 / 10.00 per million
        assert rates == (2.50, 10.00)

    def test_override_case_insensitive(self):
        from cms.services.assistant.pricing import _resolve_model

        key, _ = _resolve_model("chat", model_override="GPT-4O")
        assert key == "gpt-4o"

    def test_unknown_override_falls_back_to_deployment_match(self):
        from cms.services.assistant.pricing import _resolve_model

        key, _ = _resolve_model("cms-gpt-4o-mini", model_override="madeup")
        assert key == "gpt-4o-mini"

    def test_estimate_usd_uses_override(self):
        from cms.services.assistant.pricing import estimate_usd

        cost = estimate_usd(
            deployment="chat",
            tokens_in=1_000_000,
            tokens_out=1_000_000,
            model_override="gpt-4o",
        )
        assert cost == pytest.approx(12.50)

    def test_model_for_deployment_uses_override(self):
        from cms.services.assistant.pricing import model_for_deployment

        assert (
            model_for_deployment("chat", model_override="gpt-4o") == "gpt-4o"
        )

    def test_empty_override_keeps_substring_behaviour(self):
        from cms.services.assistant.pricing import _resolve_model

        key, _ = _resolve_model("cms-gpt-4o-mini", model_override="")
        assert key == "gpt-4o-mini"


# ── Endpoint tests ────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestUsageEndpoint:
    async def test_zero_for_fresh_user(self, client):
        resp = await client.get("/api/chat/usage")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["used_tokens"] == 0
        assert body["used_tokens_in"] == 0
        assert body["used_tokens_out"] == 0
        assert body["used_usd_estimate"] == 0.0
        assert body["cap_tokens"] > 0  # default cap applies
        assert body["unlimited"] is False
        # The deployment configured for tests may not match our table —
        # just assert the field exists and is a string.
        assert isinstance(body["model"], str)

    async def test_sums_todays_tokens(self, app, client):
        tid = await _create_thread(client)
        await _seed_usage(app, tid, 1234, 5678)

        resp = await client.get("/api/chat/usage")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["used_tokens_in"] == 1234
        assert body["used_tokens_out"] == 5678
        assert body["used_tokens"] == 1234 + 5678
        # USD must be > 0 once tokens are non-zero, regardless of
        # whether the deployment matched the table (fallback rates
        # are also non-zero).
        assert body["used_usd_estimate"] > 0.0

    async def test_unlimited_flag_when_cap_negative(self, app, client):
        from cms.database import get_db
        from cms.services.assistant.budget import set_default_cap

        factory = app.dependency_overrides[get_db]
        async for db in factory():
            await set_default_cap(db, -1)
            break

        resp = await client.get("/api/chat/usage")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["unlimited"] is True
        assert body["cap_tokens"] == -1
