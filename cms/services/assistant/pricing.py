"""Token-to-USD estimation for the Assistant.

The per-user daily token cap (:mod:`cms.services.assistant.budget`) is
denominated in tokens because that's what the Azure OpenAI API returns
deterministically.  Operators (and end users, via the in-page usage
strip) generally think in dollars, so this module maintains a small
lookup table of ``(input_$/1M-tokens, output_$/1M-tokens)`` for the
Azure OpenAI models we actually deploy, plus a fallback for any model
that isn't in the table.

These rates are **list prices** taken from the Azure OpenAI pricing
page (or OpenAI's, where Azure mirrors them).  They are estimates:

* Discounts from EA / committed-use don't apply here.
* Cached input tokens are billed at a different rate in some models
  — we don't have visibility into the cache hit rate, so we use the
  full input rate.
* Tool / function tokens are billed at the input rate (correct).

The fallback rate matches gpt-4o-mini (the cheapest modern small
model).  This may under-report cost if the unknown deployment is
actually a frontier model, but it avoids scaring users with phantom
$$$ when the deployment name simply doesn't follow our convention.
See :data:`FALLBACK_RATES_USD_PER_M_TOKENS`.

To update rates after a price change:

* Edit :data:`PRICE_TABLE_USD_PER_M_TOKENS`.
* Bump :data:`PRICE_TABLE_VERSION` so any cached UI strings refresh.
* The deployment-name → model-key heuristic in :func:`_resolve_model`
  is intentionally loose; the matcher prefers the longest substring
  match so ``gpt-4o-mini-2024-07-18`` resolves to ``gpt-4o-mini``
  rather than ``gpt-4o``.
"""

from __future__ import annotations

from typing import Final

# Rates are USD per 1,000,000 tokens, ``(input, output)``.
#
# Sources (as of 2025-Q2):
#   * https://azure.microsoft.com/en-us/pricing/details/cognitive-services/openai-service/
#   * https://openai.com/api/pricing/
#
# Keep keys lowercase; matching is case-insensitive.
PRICE_TABLE_USD_PER_M_TOKENS: Final[dict[str, tuple[float, float]]] = {
    # GPT-4o family
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    # GPT-4 turbo / classic
    "gpt-4-turbo": (10.00, 30.00),
    "gpt-4": (30.00, 60.00),
    # GPT-3.5
    "gpt-35-turbo": (0.50, 1.50),
    "gpt-3.5-turbo": (0.50, 1.50),
    # o1 / o3 reasoning models
    "o1": (15.00, 60.00),
    "o1-mini": (3.00, 12.00),
    "o3-mini": (1.10, 4.40),
    "o4-mini": (1.10, 4.40),
}

# Fallback when the configured deployment doesn't match anything above.
# We pick gpt-4o-mini's rate as a conservative-but-not-doom default:
# small enough that we don't scare users with phantom $$$, but if it
# turns out the deployment is gpt-4o the under-report is at worst ~16×.
FALLBACK_RATES_USD_PER_M_TOKENS: Final[tuple[float, float]] = (0.15, 0.60)

# Bump on rate-table edits so the UI can cache-bust if needed.
PRICE_TABLE_VERSION: Final[int] = 1


def _resolve_model(deployment: str) -> tuple[str, tuple[float, float]]:
    """Match a deployment name to a price-table entry.

    Azure OpenAI deployments are user-named (e.g. ``cms-chat-prod``)
    but the *model* the deployment points at is typically embedded in
    the name (e.g. ``cms-gpt-4o-mini``).  We do a longest-substring
    match so that ``gpt-4o-mini-2024-07-18`` and ``my-gpt-4o-mini-v2``
    both resolve correctly.

    Returns ``(matched_key_or_'unknown', rates)``.  Use the matched
    key when emitting telemetry / UI strings so the operator can see
    which entry was chosen.
    """
    if not deployment:
        return "unknown", FALLBACK_RATES_USD_PER_M_TOKENS
    dep = deployment.lower()
    best_key: str | None = None
    for key in PRICE_TABLE_USD_PER_M_TOKENS:
        if key in dep and (best_key is None or len(key) > len(best_key)):
            best_key = key
    if best_key is None:
        return "unknown", FALLBACK_RATES_USD_PER_M_TOKENS
    return best_key, PRICE_TABLE_USD_PER_M_TOKENS[best_key]


def estimate_usd(
    *,
    deployment: str,
    tokens_in: int,
    tokens_out: int,
) -> float:
    """Return an estimated cost in USD for the given token counts.

    The math is trivially ``(in * in_rate + out * out_rate) / 1e6``.
    Negative inputs are clamped to zero so a corrupt row in the
    summing query can't yield a negative cost.
    """
    tokens_in = max(0, int(tokens_in))
    tokens_out = max(0, int(tokens_out))
    _key, (in_rate, out_rate) = _resolve_model(deployment)
    return (tokens_in * in_rate + tokens_out * out_rate) / 1_000_000.0


def model_for_deployment(deployment: str) -> str:
    """Return the matched price-table model key (or ``"unknown"``).

    Useful for the ``/api/chat/usage`` payload so the UI can show
    operators which rate row was applied to their numbers.
    """
    key, _ = _resolve_model(deployment)
    return key
