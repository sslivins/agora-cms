"""Azure AI Speech client wrapper for Voice Announcements synthesis."""

from __future__ import annotations

import logging
from typing import Any
from xml.sax.saxutils import escape

import httpx
from azure.identity.aio import DefaultAzureCredential, get_bearer_token_provider

from shared.config import SharedSettings

logger = logging.getLogger(__name__)

_SPEECH_SCOPE = "https://cognitiveservices.azure.com/.default"
_OUTPUT_FORMAT = "ogg-48khz-16bit-mono-opus"


class SpeechUnavailableError(RuntimeError):
    """Raised when Azure AI Speech isn't configured in this environment."""


def is_available(settings: SharedSettings) -> bool:
    """Return True iff Azure AI Speech is wired up in this environment."""
    return bool(settings.azure_speech_endpoint and settings.azure_speech_region)


def _escape_attr(value: str) -> str:
    return escape(value, {'"': "&quot;", "'": "&apos;"})


def _build_ssml(
    script_text: str,
    *,
    voice_name: str,
    emotion: str | None,
    language: str,
    speech_rate: str | None,
) -> str:
    content = escape(script_text, {'"': "&quot;", "'": "&apos;"})
    if speech_rate:
        content = f'<prosody rate="{_escape_attr(speech_rate)}">{content}</prosody>'
    if emotion:
        content = (
            f'<mstts:express-as style="{_escape_attr(emotion)}">'
            f"{content}</mstts:express-as>"
        )
    return (
        '<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" '
        'xmlns:mstts="https://www.w3.org/2001/mstts" '
        f'xml:lang="{_escape_attr(language)}">'
        f'<voice name="{_escape_attr(voice_name)}">{content}</voice>'
        "</speak>"
    )


class SpeechClient:
    """Async wrapper around the Azure AI Speech REST TTS endpoint."""

    def __init__(self, settings: SharedSettings) -> None:
        if not is_available(settings):
            raise SpeechUnavailableError(
                "Azure AI Speech is not configured "
                "(AGORA_CMS_AZURE_SPEECH_ENDPOINT / _REGION unset)."
            )
        self._settings = settings
        self._credential = DefaultAzureCredential()
        self._token_provider = get_bearer_token_provider(
            self._credential, _SPEECH_SCOPE
        )
        self._client = httpx.AsyncClient(timeout=60.0)
        self._synthesis_url = (
            f"https://{settings.azure_speech_region}.tts.speech.microsoft.com/"
            "cognitiveservices/v1"
        )

    async def aclose(self) -> None:
        await self._client.aclose()
        await self._credential.close()

    async def __aenter__(self) -> "SpeechClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def synthesize(
        self,
        script_text: str,
        *,
        voice_name: str,
        emotion: str | None,
        language: str,
        speech_rate: str | None,
    ) -> bytes:
        """Synthesize the given script to Ogg/Opus audio bytes."""
        ssml = _build_ssml(
            script_text,
            voice_name=voice_name,
            emotion=emotion,
            language=language,
            speech_rate=speech_rate,
        )
        token = await self._token_provider()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/ssml+xml",
            "X-Microsoft-OutputFormat": _OUTPUT_FORMAT,
        }
        response = await self._client.post(
            self._synthesis_url,
            headers=headers,
            content=ssml.encode("utf-8"),
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text.strip()
            raise RuntimeError(
                f"Azure AI Speech synthesis failed: {exc.response.status_code}"
                + (f" {detail}" if detail else "")
            ) from exc
        logger.info(
            "voice_announcement.speech_synthesized region=%s bytes=%d voice=%s",
            self._settings.azure_speech_region,
            len(response.content),
            voice_name,
        )
        return response.content
