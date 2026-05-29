"""Azure OpenAI client wrapper.

Thin async facade over the official ``openai`` SDK's ``AsyncAzureOpenAI``
client.  Encapsulates:

* Endpoint / deployment / api-version lookup from :class:`cms.config.Settings`.
* Managed-identity authentication via :class:`azure.identity.DefaultAzureCredential`
  + the ``cognitiveservices.azure.com/.default`` scope (matches the
  ``Cognitive Services OpenAI User`` role granted to the CMS managed
  identity by ``infra/main.bicep``).
* "Feature available?" check — returns ``False`` when the endpoint or
  deployment env var is empty so callers can degrade cleanly in envs
  that haven't opted into Azure OpenAI.

The client itself is intentionally stateless and **not** cached on the
process — the underlying SDK keeps its own ``httpx`` connection pool and
the credential object is cheap to construct.  If profiling later shows
auth overhead we can memoise; for the single-shot PR 3a flow there's no
point.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from azure.identity.aio import DefaultAzureCredential, get_bearer_token_provider
from openai import AsyncAzureOpenAI

from cms.config import Settings

logger = logging.getLogger(__name__)


# AAD scope for Azure Cognitive Services data-plane.  See:
# https://learn.microsoft.com/azure/ai-services/openai/how-to/managed-identity
_AOAI_SCOPE = "https://cognitiveservices.azure.com/.default"


class AssistantUnavailableError(RuntimeError):
    """Raised when Azure OpenAI isn't configured in this environment.

    The chat router catches this and converts it to ``503 Service
    Unavailable`` so allowlisted users get a clear error message rather
    than a 500.  This is distinct from the feature-flag gate, which
    404s; here the feature IS enabled, the LLM backend just isn't
    deployed.
    """


@dataclass
class CompletionResult:
    """Return value of :meth:`LLMClient.complete`."""

    content: str
    tokens_in: int
    tokens_out: int


def is_available(settings: Settings) -> bool:
    """Return True iff Azure OpenAI is wired up in this environment."""
    return bool(settings.azure_openai_endpoint and settings.azure_openai_deployment)


class LLMClient:
    """Async wrapper around :class:`openai.AsyncAzureOpenAI`."""

    def __init__(self, settings: Settings) -> None:
        if not is_available(settings):
            raise AssistantUnavailableError(
                "Azure OpenAI is not configured "
                "(AGORA_CMS_AZURE_OPENAI_ENDPOINT / _DEPLOYMENT unset)."
            )
        self._settings = settings
        # Per-instance credential — the openai SDK calls the token
        # provider on every request, so reusing one credential lets
        # MSAL cache hits short-circuit the AAD round-trip.
        self._credential = DefaultAzureCredential()
        token_provider = get_bearer_token_provider(self._credential, _AOAI_SCOPE)
        self._client = AsyncAzureOpenAI(
            azure_endpoint=settings.azure_openai_endpoint,
            azure_ad_token_provider=token_provider,
            api_version=settings.azure_openai_api_version,
        )

    async def aclose(self) -> None:
        """Release the SDK's httpx pool and the AAD credential."""
        await self._client.close()
        await self._credential.close()

    async def __aenter__(self) -> "LLMClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        max_completion_tokens: int | None = None,
        temperature: float = 0.2,
    ) -> CompletionResult:
        """Run a single non-streaming chat completion.

        ``messages`` is the OpenAI-format conversation (``{role, content}``
        dicts), pre-trimmed by the caller — this method does not enforce
        a context-window budget.

        Returns the assembled assistant content and token usage.  If the
        API returns ``None`` for ``content`` (e.g. content-filter
        response) we substitute an empty string so the caller can still
        persist the turn; the absence of content is recorded as-is.
        """
        max_tokens = (
            max_completion_tokens
            if max_completion_tokens is not None
            else self._settings.assistant_max_completion_tokens
        )
        logger.info(
            "assistant.llm.request deployment=%s messages=%d max_tokens=%d",
            self._settings.azure_openai_deployment,
            len(messages),
            max_tokens,
        )
        response = await self._client.chat.completions.create(
            model=self._settings.azure_openai_deployment,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        choice = response.choices[0]
        content = choice.message.content or ""
        usage = response.usage
        tokens_in = getattr(usage, "prompt_tokens", 0) if usage else 0
        tokens_out = getattr(usage, "completion_tokens", 0) if usage else 0
        logger.info(
            "assistant.llm.response finish=%s in=%d out=%d",
            choice.finish_reason,
            tokens_in,
            tokens_out,
        )
        return CompletionResult(
            content=content, tokens_in=tokens_in, tokens_out=tokens_out
        )
