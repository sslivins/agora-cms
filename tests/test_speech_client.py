"""Tests for the Azure AI Speech client wrapper."""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

from cms.config import Settings
from cms.services import speech_client as speech_client_module
from cms.services.speech_client import SpeechClient, SpeechUnavailableError, is_available


class _FakeCredential:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_speech_client_unavailable_raises():
    settings = Settings(
        database_url="sqlite:///x",
        secret_key="x",
        azure_speech_endpoint="",
        azure_speech_region="",
    )
    with pytest.raises(SpeechUnavailableError):
        SpeechClient(settings)


def test_speech_client_is_available_helper():
    settings = Settings(database_url="sqlite:///x", secret_key="x")
    assert is_available(settings) is False
    settings.azure_speech_endpoint = "https://example.cognitiveservices.azure.com/"
    settings.azure_speech_region = "westus"
    assert is_available(settings) is True


@pytest.mark.asyncio
async def test_synthesize_builds_expected_ssml_and_headers():
    seen: dict[str, object] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["body"] = request.content.decode("utf-8")
        return httpx.Response(200, content=b"fake-ogg-audio")

    transport = httpx.MockTransport(_handler)
    real_client = httpx.AsyncClient
    fake_credential = _FakeCredential()

    def _client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    async def _token_provider():
        return "fake-token"

    settings = Settings(
        database_url="sqlite:///x",
        secret_key="x",
        azure_speech_endpoint="https://example.cognitiveservices.azure.com/",
        azure_speech_region="westus",
    )

    with patch("cms.services.speech_client.DefaultAzureCredential", return_value=fake_credential), \
         patch("cms.services.speech_client.get_bearer_token_provider", return_value=_token_provider), \
         patch("cms.services.speech_client.httpx.AsyncClient", side_effect=_client_factory):
        async with SpeechClient(settings) as client:
            data = await client.synthesize(
                'Hello & welcome <team> "friends" \'neighbors\'',
                voice_name="en-US-Ava:MAI-Voice-2",
                emotion="cheerful",
                language="en-US",
                speech_rate="+10%",
            )

    assert data == b"fake-ogg-audio"
    assert seen["url"] == (
        "https://example.cognitiveservices.azure.com/tts/cognitiveservices/v1"
    )
    headers = httpx.Headers(seen["headers"])
    assert headers["Authorization"] == "Bearer fake-token"
    assert headers["Content-Type"] == "application/ssml+xml"
    assert headers["X-Microsoft-OutputFormat"] == "ogg-48khz-16bit-mono-opus"
    body = str(seen["body"])
    assert 'voice name="en-US-Ava:MAI-Voice-2"' in body
    assert '<mstts:express-as style="cheerful">' in body
    assert '<prosody rate="+10%">' in body
    assert "Hello &amp; welcome &lt;team&gt; &quot;friends&quot; &apos;neighbors&apos;" in body
    assert fake_credential.closed is True


@pytest.mark.asyncio
async def test_list_voices_filters_and_caches():
    seen: dict[str, int] = {"calls": 0}

    def _handler(request: httpx.Request) -> httpx.Response:
        seen["calls"] += 1
        assert str(request.url) == (
            "https://example.cognitiveservices.azure.com/tts/cognitiveservices/voices/list"
        )
        return httpx.Response(
            200,
            json=[
                {
                    "ShortName": "en-US-Ava:MAI-Voice-2",
                    "DisplayName": "Ava",
                    "Locale": "en-US",
                    "VoiceType": "MAI-Voice-2",
                    "StyleList": ["cheerful", "sad", "cheerful"],
                },
                {
                    # Azure publishes a latency-optimised twin for every
                    # MAI-Voice-2 voice. Announcements are generated ahead
                    # of playback, so the twin buys us nothing and only
                    # doubles the dropdown -- it must be filtered out.
                    "ShortName": "en-US-Ava:MAI-Voice-2-Flash",
                    "DisplayName": "Ava MAI-Voice-2-Flash",
                    "Locale": "en-US",
                    "VoiceType": "MAI-Voice-2",
                    "StyleList": ["cheerful", "sad"],
                },
                {
                    "ShortName": "en-GB-Old",
                    "DisplayName": "Old",
                    "Locale": "en-GB",
                    "VoiceType": "Standard",
                },
            ],
        )

    transport = httpx.MockTransport(_handler)
    real_client = httpx.AsyncClient
    fake_credential = _FakeCredential()

    def _client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    async def _token_provider():
        return "fake-token"

    settings = Settings(
        database_url="sqlite:///x",
        secret_key="x",
        azure_speech_endpoint="https://example.cognitiveservices.azure.com/",
        azure_speech_region="westus",
    )

    speech_client_module._VOICE_LIST_CACHE.clear()
    with patch("cms.services.speech_client.DefaultAzureCredential", return_value=fake_credential), \
         patch("cms.services.speech_client.get_bearer_token_provider", return_value=_token_provider), \
         patch("cms.services.speech_client.httpx.AsyncClient", side_effect=_client_factory):
        async with SpeechClient(settings) as client:
            first = await client.list_voices(language="en-US")
            second = await client.list_voices(language="en-US")

    assert first == [
        {
            "short_name": "en-US-Ava:MAI-Voice-2",
            "display_name": "Ava",
            "locale": "en-US",
            "emotions": ["cheerful", "sad"],
        }
    ]
    assert second == first
    assert seen["calls"] == 1
    assert fake_credential.closed is True
    assert not any("Flash" in v["short_name"] for v in first)
