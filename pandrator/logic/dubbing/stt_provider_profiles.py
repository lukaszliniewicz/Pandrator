"""Static profiles for supported remote speech-to-text providers.

The catalogue is deliberately data-only.  Credentials, endpoint hydration, and
provider connection records belong to the surrounding application layers; the
runtime consumes these profiles as an engine-neutral request description.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

STT_ENGINE_AZURE_MAI_TRANSCRIBE_1_5 = "azure_mai_transcribe_1_5"
STT_ENGINE_AZURE_MAI_TRANSCRIBE_2 = "azure_mai_transcribe_2"
CLOUD_STT_ENGINE_IDS = frozenset(
    {
        STT_ENGINE_AZURE_MAI_TRANSCRIBE_2,
        STT_ENGINE_AZURE_MAI_TRANSCRIBE_1_5,
    }
)


# MAI-Transcribe-1.5 accepts language codes. Keep the list in the profile so
# callers can build language selectors without coupling themselves to Azure's
# request format.  ``auto`` is represented by omission from the request, not
# as a model locale.
AZURE_MAI_TRANSCRIBE_1_5_LOCALES = (
    "ar",
    "as",
    "bg",
    "bn",
    "ca",
    "cs",
    "da",
    "de",
    "el",
    "en",
    "es",
    "et",
    "fi",
    "fr",
    "gu",
    "hi",
    "hu",
    "id",
    "it",
    "ja",
    "kn",
    "ko",
    "lt",
    "ml",
    "mr",
    "nb",
    "nl",
    "or",
    "pa",
    "pl",
    "pt",
    "ro",
    "ru",
    "sk",
    "sl",
    "sv",
    "ta",
    "te",
    "th",
    "tr",
    "uk",
    "vi",
    "zh",
)
AZURE_MAI_TRANSCRIBE_2_LOCALES = (
    "af",
    "ar",
    "as",
    "az",
    "bg",
    "bn",
    "bs",
    "ca",
    "cs",
    "da",
    "de",
    "el",
    "en",
    "es",
    "et",
    "fa",
    "fi",
    "fil",
    "fr",
    "gl",
    "gu",
    "he",
    "hi",
    "hu",
    "hy",
    "id",
    "is",
    "it",
    "ja",
    "kk",
    "kn",
    "ko",
    "lt",
    "lv",
    "mk",
    "ml",
    "mr",
    "ms",
    "nb",
    "ne",
    "nl",
    "or",
    "pa",
    "pl",
    "pt",
    "ro",
    "ru",
    "sk",
    "sl",
    "sv",
    "sw",
    "ta",
    "te",
    "th",
    "tr",
    "uk",
    "ur",
    "vi",
    "yue",
    "zh",
)


def _azure_upload_limit() -> dict[str, Any]:
    """Return the conservative, unresolved Azure upload-limit description."""

    return {
        "max_bytes": None,
        "max_duration_seconds": None,
        "hard_reject": False,
        "confidence": "conflicting",
        "notes": (
            "Microsoft documentation has conflicting upload-size and duration limits; "
            "automatic chunking uses the conservative REST limit while the client does "
            "not reject audio locally based on an inferred whole-file limit."
        ),
        "conservative_max_bytes": 250_000_000,
        "conservative_max_duration_seconds": 7_200,
    }


def _azure_mai_transcribe_2_profile() -> dict[str, Any]:
    return {
        "id": STT_ENGINE_AZURE_MAI_TRANSCRIBE_2,
        "engine": STT_ENGINE_AZURE_MAI_TRANSCRIBE_2,
        "name": "Azure Speech · MAI-Transcribe-2",
        "label": "Azure Speech · MAI-Transcribe-2",
        "description": "Azure Speech synchronous transcription with native word timing.",
        "provider": "azure",
        "adapter": "azure_speech_fast_transcription",
        "api_base": "https://YOUR-RESOURCE-NAME.cognitiveservices.azure.com",
        "base_url": "https://YOUR-RESOURCE-NAME.cognitiveservices.azure.com",
        "path": "/speechtotext/transcriptions:transcribe?api-version=2025-10-15",
        "transcription_path": "/speechtotext/transcriptions:transcribe?api-version=2025-10-15",
        "model": "MAI-Transcribe-2",
        "models": ["MAI-Transcribe-2"],
        "api_key_env": "AZURE_SPEECH_KEY",
        "credential_required": True,
        "auth_mode": "subscription-key",
        "auth_header_mode": "subscription-key",
        "auth_scheme": "subscription_key",
        "auth_header": "Ocp-Apim-Subscription-Key",
        "auth": {
            "mode": "subscription_key",
            "header": "Ocp-Apim-Subscription-Key",
        },
        "word_timestamps": True,
        "word_timing": "native",
        "diarization": False,
        "supports_diarization": False,
        "remote": True,
        "execution_mode": "synchronous",
        "synchronous": True,
        "supported_locales": list(AZURE_MAI_TRANSCRIBE_2_LOCALES),
        "locales": list(AZURE_MAI_TRANSCRIBE_2_LOCALES),
        "languages": list(AZURE_MAI_TRANSCRIBE_2_LOCALES),
        "regions": ["eastus", "northeurope", "southeastasia", "westus"],
        "settings": {
            "stt_transcribe_style": "clean",
            "stt_cloud_max_chunk_seconds": 5_400,
            "stt_cloud_chunk_search_seconds": 300,
            "stt_cloud_min_silence_ms": 1_500,
        },
        "pricing": {
            "unit": "audio_hour",
            "source": "published_list_price_estimate",
            "source_url": "https://techcommunity.microsoft.com/blog/azure-ai-foundry-blog/mai-transcribe-2-highest-quality-transcription-at-the-fastest-speed-and-lowest-c/4550972",
            "amount_usd": 0.10,
            "currency": "USD",
            "estimate_only": True,
            "price_effective_until": "2026-12-31",
        },
        "upload_limit": _azure_upload_limit(),
        "upload_limits": {
            "max_bytes": None,
            "max_duration_seconds": None,
            "hard_reject": False,
            "confidence": "conflicting",
        },
        "upload_limit_bytes": None,
        "upload_limit_confidence": "conflicting",
        "source_url": "https://techcommunity.microsoft.com/blog/azure-ai-foundry-blog/mai-transcribe-2-highest-quality-transcription-at-the-fastest-speed-and-lowest-c/4550972",
        "source_urls": [
            "https://techcommunity.microsoft.com/blog/azure-ai-foundry-blog/mai-transcribe-2-highest-quality-transcription-at-the-fastest-speed-and-lowest-c/4550972",
            "https://learn.microsoft.com/rest/api/speechtotext/transcriptions/transcribe?view=rest-speechtotext-2025-10-15",
        ],
        "documentation_urls": [
            "https://learn.microsoft.com/azure/ai-services/speech-service/mai-transcribe",
            "https://learn.microsoft.com/rest/api/speechtotext/transcriptions/transcribe?view=rest-speechtotext-2025-10-15",
        ],
    }


def _azure_profile() -> dict[str, Any]:
    return {
        "id": STT_ENGINE_AZURE_MAI_TRANSCRIBE_1_5,
        "engine": STT_ENGINE_AZURE_MAI_TRANSCRIBE_1_5,
        "name": "Azure Speech · MAI-Transcribe-1.5",
        "label": "Azure Speech · MAI-Transcribe-1.5",
        "description": "Azure Speech synchronous fast transcription with word timing.",
        "provider": "azure",
        "adapter": "azure_speech_fast_transcription",
        "api_base": "https://YOUR-RESOURCE-NAME.cognitiveservices.azure.com",
        "base_url": "https://YOUR-RESOURCE-NAME.cognitiveservices.azure.com",
        "path": "/speechtotext/transcriptions:transcribe?api-version=2025-10-15",
        "transcription_path": "/speechtotext/transcriptions:transcribe?api-version=2025-10-15",
        "model": "mai-transcribe-1.5",
        "models": ["mai-transcribe-1.5"],
        "api_key_env": "AZURE_SPEECH_KEY",
        "credential_required": True,
        "auth_mode": "subscription-key",
        "auth_header_mode": "subscription-key",
        "auth_scheme": "subscription_key",
        "auth_header": "Ocp-Apim-Subscription-Key",
        "auth": {
            "mode": "subscription_key",
            "header": "Ocp-Apim-Subscription-Key",
        },
        "word_timestamps": True,
        "word_timing": "native",
        "diarization": False,
        "supports_diarization": False,
        "remote": True,
        "execution_mode": "synchronous",
        "synchronous": True,
        "supported_locales": list(AZURE_MAI_TRANSCRIBE_1_5_LOCALES),
        "locales": list(AZURE_MAI_TRANSCRIBE_1_5_LOCALES),
        "languages": list(AZURE_MAI_TRANSCRIBE_1_5_LOCALES),
        "regions": ["eastus", "northeurope", "southeastasia", "westus"],
        "settings": {
            "stt_transcribe_style": "readability",
            "stt_cloud_max_chunk_seconds": 5_400,
            "stt_cloud_chunk_search_seconds": 300,
            "stt_cloud_min_silence_ms": 1_500,
        },
        "pricing": {
            "unit": "audio_hour",
            "source": "azure_speech_pricing",
            "amount_usd": None,
        },
        "upload_limit": _azure_upload_limit(),
        "upload_limits": {
            "max_bytes": None,
            "max_duration_seconds": None,
            "hard_reject": False,
            "confidence": "conflicting",
        },
        "upload_limit_bytes": None,
        "upload_limit_confidence": "conflicting",
        "source_url": "https://learn.microsoft.com/azure/ai-services/speech-service/mai-transcribe",
        "source_urls": [
            "https://learn.microsoft.com/azure/ai-services/speech-service/mai-transcribe",
            "https://learn.microsoft.com/rest/api/speechtotext/transcriptions/transcribe?view=rest-speechtotext-2025-10-15",
        ],
        "documentation_urls": [
            "https://learn.microsoft.com/azure/ai-services/speech-service/mai-transcribe",
            "https://learn.microsoft.com/rest/api/speechtotext/transcriptions/transcribe?view=rest-speechtotext-2025-10-15",
        ],
    }


# OpenAI's current documentation exposes word granularities for ``whisper-1``
# only.  ``gpt-transcribe`` is intentionally absent until its exact timed-word
# contract is documented; the runtime must not present a pseudo-provider.
STT_PROVIDER_PROFILES: list[dict[str, Any]] = [
    _azure_mai_transcribe_2_profile(),
    _azure_profile(),
]


def list_stt_provider_profiles() -> list[dict[str, Any]]:
    """Return an isolated copy of every built-in remote STT profile."""

    return deepcopy(STT_PROVIDER_PROFILES)


def get_stt_provider_profile(profile_id: str) -> dict[str, Any] | None:
    """Return an isolated profile by its stable engine identifier."""

    normalized_id = str(profile_id or "").strip().lower()
    for profile in STT_PROVIDER_PROFILES:
        if profile["id"].lower() == normalized_id:
            return deepcopy(profile)
    return None


def is_cloud_stt_engine(engine: str | None) -> bool:
    """Return whether *engine* is one of the built-in remote providers."""

    normalized = str(engine or "").strip().lower().replace("-", "_").replace(" ", "_")
    return normalized in CLOUD_STT_ENGINE_IDS


__all__ = [
    "AZURE_MAI_TRANSCRIBE_1_5_LOCALES",
    "AZURE_MAI_TRANSCRIBE_2_LOCALES",
    "CLOUD_STT_ENGINE_IDS",
    "STT_ENGINE_AZURE_MAI_TRANSCRIBE_1_5",
    "STT_ENGINE_AZURE_MAI_TRANSCRIBE_2",
    "STT_PROVIDER_PROFILES",
    "get_stt_provider_profile",
    "is_cloud_stt_engine",
    "list_stt_provider_profiles",
]
