"""Regression test for the streaming-usage capture in :class:`LLMClient`.

Azure OpenAI emits the final ``usage`` block in a chunk *after* the
chunk that carries ``finish_reason``.  An earlier bug yielded the
``finish`` event inline (with tokens=0) when ``finish_reason`` showed
up, then skipped the later usage-only chunk because ``choices == []``.
The net effect: every streamed turn persisted ``tokens_in/out = 0``
and the per-user usage strip on the Assistant page always read
"0 tokens".

This test pins the corrected behaviour: a single ``finish`` event is
yielded after the stream ends, carrying both the reason and the real
token counts.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from cms.services.assistant.llm_client import LLMClient


class _FakeChatCompletions:
    """Minimal stand-in for ``client.chat.completions``."""

    def __init__(self, chunks: list[Any]) -> None:
        self._chunks = chunks
        self.last_kwargs: dict[str, Any] | None = None

    async def create(self, **kwargs: Any) -> Any:
        self.last_kwargs = kwargs

        async def _iter():
            for c in self._chunks:
                yield c

        return _iter()


def _make_chunk(
    *,
    content: str | None = None,
    finish_reason: str | None = None,
    usage: tuple[int, int] | None = None,
) -> SimpleNamespace:
    """Build a chunk shaped like ``openai.types.chat.ChatCompletionChunk``.

    ``usage`` is a ``(prompt_tokens, completion_tokens)`` tuple; when
    set we emit a usage-only chunk with ``choices=[]`` to mimic AOAI's
    real stream-termination pattern.
    """
    if usage is not None:
        return SimpleNamespace(
            choices=[],
            usage=SimpleNamespace(prompt_tokens=usage[0], completion_tokens=usage[1]),
        )
    delta = SimpleNamespace(content=content, tool_calls=None)
    choice = SimpleNamespace(delta=delta, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], usage=None)


def _make_client(chunks: list[Any]) -> LLMClient:
    """Construct an ``LLMClient`` without touching real Azure auth."""
    client = object.__new__(LLMClient)  # bypass __init__
    client._settings = SimpleNamespace(
        azure_openai_deployment="chat",
        assistant_max_completion_tokens=512,
    )
    fake = SimpleNamespace(chat=SimpleNamespace(completions=_FakeChatCompletions(chunks)))
    client._client = fake
    return client


@pytest.mark.asyncio
async def test_stream_captures_usage_from_trailing_chunk():
    """The canonical AOAI sequence: content → finish-reason → usage-only.

    The fix must defer the single ``finish`` yield until after the
    stream ends so it carries the real tokens_in/out.
    """
    chunks = [
        _make_chunk(content="Hello"),
        _make_chunk(content=" world"),
        _make_chunk(finish_reason="stop"),
        _make_chunk(usage=(42, 17)),
    ]
    client = _make_client(chunks)
    events = [ev async for ev in client.stream(messages=[{"role": "user", "content": "hi"}])]

    finishes = [e for e in events if e["type"] == "finish"]
    assert len(finishes) == 1, f"expected exactly one finish event, got {events!r}"
    finish = finishes[0]
    assert finish["reason"] == "stop"
    assert finish["tokens_in"] == 42
    assert finish["tokens_out"] == 17

    texts = [e["text"] for e in events if e["type"] == "content"]
    assert "".join(texts) == "Hello world"


@pytest.mark.asyncio
async def test_stream_finish_without_usage_yields_zero_tokens():
    """Older API versions / non-streaming-usage paths still terminate
    cleanly — we just report zeros instead of dropping the event."""
    chunks = [
        _make_chunk(content="hi"),
        _make_chunk(finish_reason="stop"),
    ]
    client = _make_client(chunks)
    events = [ev async for ev in client.stream(messages=[{"role": "user", "content": "hi"}])]

    finishes = [e for e in events if e["type"] == "finish"]
    assert len(finishes) == 1
    assert finishes[0]["reason"] == "stop"
    assert finishes[0]["tokens_in"] == 0
    assert finishes[0]["tokens_out"] == 0


@pytest.mark.asyncio
async def test_stream_usage_with_zero_completion_does_not_clobber_prompt():
    """Guard against the ``or`` short-circuit losing a real value when
    the other side of the usage tuple is zero."""
    chunks = [
        _make_chunk(content="x"),
        _make_chunk(finish_reason="stop"),
        _make_chunk(usage=(15, 0)),
    ]
    client = _make_client(chunks)
    events = [ev async for ev in client.stream(messages=[{"role": "user", "content": "hi"}])]
    finish = next(e for e in events if e["type"] == "finish")
    assert finish["tokens_in"] == 15
    assert finish["tokens_out"] == 0
