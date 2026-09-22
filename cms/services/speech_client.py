"""Azure AI Speech client wrapper for Voice Announcements synthesis."""

from __future__ import annotations

import logging
import time
from typing import Any
from xml.sax.saxutils import escape

import httpx
from azure.identity.aio import DefaultAzureCredential, get_bearer_token_provider

from shared.config import SharedSettings

logger = logging.getLogger(__name__)

_SPEECH_SCOPE = "https://cognitiveservices.azure.com/.default"
_OUTPUT_FORMAT = "ogg-48khz-16bit-mono-opus"
_VOICE_LIST_TTL_SECONDS = 600
_VOICE_LIST_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}


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


def _voice_cache_key(language: str | None) -> str:
    return (language or "").strip().lower()


def _copy_voice_list(voices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "short_name": str(v["short_name"]),
            "display_name": str(v["display_name"]),
            "locale": str(v["locale"]),
            "emotions": list(v.get("emotions", [])),
        }
        for v in voices
    ]


def _locale_matches_requested(locale: str | None, language: str | None) -> bool:
    if not language:
        return True
    if not locale:
        return False
    requested = language.strip().lower()
    candidate = locale.strip().lower()
    return candidate.startswith(requested)


def _entry_has_mai_voice_2_marker(entry: dict[str, Any]) -> bool:
    for key in ("VoiceType", "Name", "ShortName", "DisplayName", "LocalName", "Model", "ModelName", "Family"):
        value = entry.get(key)
        if isinstance(value, str):
            lowered = value.lower()
            if "mai-voice-2" in lowered or "mai voice 2" in lowered:
                return True
    return False


def _simplify_voices_payload(
    payload: list[dict[str, Any]],
    *,
    language: str | None,
) -> list[dict[str, Any]]:
    has_explicit_mai_marker = any(_entry_has_mai_voice_2_marker(entry) for entry in payload)
    simplified: list[dict[str, Any]] = []

    for entry in payload:
        locale = entry.get("Locale")
        if not isinstance(locale, str) or not locale.strip():
            continue

        if has_explicit_mai_marker:
            if not _entry_has_mai_voice_2_marker(entry):
                continue
        else:
            # Azure's live ``voices/list`` response does not appear to
            # publish a stable MAI-Voice-2 family field in every
            # environment. When the response carries no explicit
            # MAI-specific marker at all, fall back to locale scoping so
            # the UI still offers a useful voice subset; this heuristic
            # should be tightened once we can inspect a deployed Speech
            # resource's real westus payload.
            if not _locale_matches_requested(locale, language):
                continue

        if not _locale_matches_requested(locale, language):
            continue

        short_name = entry.get("ShortName") or entry.get("Name")
        if not isinstance(short_name, str) or not short_name.strip():
            continue

        display_name = entry.get("DisplayName") or entry.get("LocalName") or short_name
        styles = entry.get("StyleList")
        emotions = sorted(
            {
                str(style).strip()
                for style in (styles if isinstance(styles, list) else [])
                if str(style).strip()
            }
        )

        simplified.append(
            {
                "short_name": short_name,
                "display_name": str(display_name),
                "locale": locale,
                "emotions": emotions,
            }
        )

    simplified.sort(key=lambda voice: (voice["locale"], voice["display_name"], voice["short_name"]))
    return simplified


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
        # Managed-identity / Microsoft Entra ID auth is only honored on the
        # resource's custom-domain endpoint (e.g.
        # https://<resource>.cognitiveservices.azure.com) -- the shared
        # regional endpoint (e.g. https://westus.tts.speech.microsoft.com)
        # only accepts subscription-key auth and returns 401 for bearer
        # tokens, even when the token/RBAC grant is otherwise valid.
        # Both TTS REST paths are served under the ``/tts`` prefix on the
        # custom-domain host; omitting it returns a bare 404.
        custom_domain = settings.azure_speech_endpoint.rstrip("/")
        self._synthesis_url = f"{custom_domain}/tts/cognitiveservices/v1"
        self._voices_url = f"{custom_domain}/tts/cognitiveservices/voices/list"

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

    async def list_voices(self, language: str | None = None) -> list[dict[str, Any]]:
        """Return a simplified, TTL-cached voice catalog for the builder UI."""
        cache_key = _voice_cache_key(language)
        now = time.monotonic()
        cached = _VOICE_LIST_CACHE.get(cache_key)
        if cached and (now - cached[0]) < _VOICE_LIST_TTL_SECONDS:
            return _copy_voice_list(cached[1])

        token = await self._token_provider()
        response = await self._client.get(
            self._voices_url,
            headers={"Authorization": f"Bearer {token}"},
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text.strip()
            raise RuntimeError(
                f"Azure AI Speech voice listing failed: {exc.response.status_code}"
                + (f" {detail}" if detail else "")
            ) from exc

        payload = response.json()
        if not isinstance(payload, list):
            raise RuntimeError("Azure AI Speech voice listing returned a non-list payload")

        voices = _simplify_voices_payload(payload, language=language)
        _VOICE_LIST_CACHE[cache_key] = (now, _copy_voice_list(voices))
        logger.info(
            "voice_announcement.voice_catalog_loaded region=%s language=%s count=%d",
            self._settings.azure_speech_region,
            language or "",
            len(voices),
        )
        return _copy_voice_list(voices)
