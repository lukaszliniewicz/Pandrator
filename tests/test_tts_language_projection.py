from __future__ import annotations

import json
from unittest import mock

from pandrator.web.tts_providers import (
    ElevenLabsAdapter,
    SileroAdapter,
    _decorate_model_language_support,
    _slim_model_catalog,
)


def _models_by_id(service: dict) -> dict[str, dict]:
    return {item["id"]: item for item in service["model_catalog"]}


def test_native_live_catalogue_languages_override_static_support_in_both_views():
    service = {
        "id": "elevenlabs",
        "provider": "elevenlabs",
        "adapter": "elevenlabs_native",
        "models": ["eleven_multilingual_v2", "eleven_flash_v2_5"],
        "default_model": "eleven_multilingual_v2",
    }

    with (
        mock.patch(
            "pandrator.web.tts_providers.tts_handler.get_elevenlabs_model_catalog",
            return_value=[
                {
                    "id": "eleven_multilingual_v2",
                    "languages": [{"language_id": "pt_BR", "name": "Portuguese"}],
                    "description": "full-view-only model details",
                }
            ],
        ),
        mock.patch(
            "pandrator.web.tts_providers.tts_handler.get_elevenlabs_voice_catalog",
            return_value=[],
        ),
    ):
        service.update(ElevenLabsAdapter("elevenlabs").enrich_catalog(service))

    _decorate_model_language_support(service)

    full = _models_by_id(service)
    compact = {item["id"]: item for item in _slim_model_catalog(service)}
    live_full = full["eleven_multilingual_v2"]["language_support"]
    live_compact = compact["eleven_multilingual_v2"]["language_support"]
    static_full = full["eleven_flash_v2_5"]["language_support"]
    static_compact = compact["eleven_flash_v2_5"]["language_support"]

    assert live_full["coverage"] == "exact"
    assert live_full["discovery"] == "provider_live"
    assert live_full["native_route"] == "elevenlabs_text_to_speech"
    assert live_full["languages"] == ["pt-br"]
    assert live_compact["languages"] == live_full["languages"]
    assert live_compact["coverage"] == live_full["coverage"]
    assert live_compact["schema_version"] == live_full["schema_version"]
    assert live_compact["catalogue_revision"] == live_full["catalogue_revision"]
    assert "sources" in live_full
    assert "sources" not in live_compact

    assert static_full["coverage"] == "exact"
    assert static_full["discovery"] == "static"
    assert len(static_full["languages"]) == 32
    assert static_compact["languages"] == static_full["languages"]


def test_silero_native_catalogue_uses_model_languages_not_voice_locales():
    service = {
        "id": "silero",
        "provider": "silero",
        "models": ["v3_en"],
        "default_model": "v3_en",
    }
    model_catalog = [{
        "id": "v3_en",
        "languages": [{"language_id": "en_US"}],
        "status": {"installed": True},
    }]
    voices = [{"id": "fr_voice", "language": "fr", "accent": "French"}]

    with (
        mock.patch(
            "pandrator.web.tts_providers.tts_handler.get_silero_model_catalog",
            return_value=model_catalog,
        ),
        mock.patch(
            "pandrator.web.tts_providers.tts_handler.get_silero_voice_catalog",
            return_value=voices,
        ),
    ):
        service.update(SileroAdapter("silero").enrich_catalog(service))

    _decorate_model_language_support(service)
    model = _models_by_id(service)["v3_en"]
    support = model["language_support"]

    assert support["coverage"] == "exact"
    assert support["discovery"] == "provider_live"
    assert support["native_route"] == "silero_audio_speech"
    assert support["languages"] == ["en-us"]
    assert "fr" not in model["supported_languages"]
    assert service["voice_metadata"]["v3_en:fr_voice"]["accent"] == "French"


def test_elevenlabs_enrichment_marks_only_language_bearing_native_rows_live():
    service = {
        "id": "elevenlabs",
        "provider": "elevenlabs",
        "adapter": "elevenlabs_native",
        "models": ["eleven_multilingual_v2", "model-without-languages"],
        "default_model": "eleven_multilingual_v2",
    }

    with (
        mock.patch(
            "pandrator.web.tts_providers.tts_handler.get_elevenlabs_model_catalog",
            return_value=[
                {
                    "id": "eleven_multilingual_v2",
                    "languages": [{"language_id": "fr-FR"}],
                },
                {"id": "model-without-languages"},
                {
                    "id": "model-with-prose-only",
                    "languages": ["English and several more"],
                },
            ],
        ),
        mock.patch(
            "pandrator.web.tts_providers.tts_handler.get_elevenlabs_voice_catalog",
            return_value=[{"voice_id": "voice", "accent": "French"}],
        ),
    ):
        service.update(ElevenLabsAdapter("elevenlabs").enrich_catalog(service))

    _decorate_model_language_support(service)
    models = _models_by_id(service)

    assert models["eleven_multilingual_v2"]["discovery"] == "provider_live"
    assert models["eleven_multilingual_v2"]["language_coverage"] == "exact"
    assert models["eleven_multilingual_v2"]["language_support"]["languages"] == [
        "fr-fr"
    ]
    assert models["model-without-languages"]["language_support"]["coverage"] == (
        "unknown"
    )
    prose_only = models["model-with-prose-only"]
    assert prose_only.get("discovery") != "provider_live"
    assert prose_only["language_support"]["coverage"] == "unknown"
    assert prose_only["supported_languages"] == []
    compact_prose = {
        item["id"]: item for item in _slim_model_catalog(service)
    }["model-with-prose-only"]
    assert compact_prose["language_support"]["languages"] == []
    assert "languages" not in compact_prose


def test_static_fallbacks_and_unknown_custom_profile_identity_are_model_scoped():
    silero_models = [
        "v3_de",
        "v3_en",
        "v3_en_indic",
        "v3_es",
        "v3_fr",
        "v3_indic",
        "v5_5_ru",
        "v5_cis_base",
        "v5_cis_base_nostress",
        "v5_cis_ext",
    ]
    assert len(silero_models) == 10
    for model_id in silero_models:
        service = {"id": "silero", "models": [model_id], "default_model": model_id}
        _decorate_model_language_support(service)
        model = service["model_catalog"][0]
        assert model["language_support"]["coverage"] == "exact"
        assert model["supported_languages"]

    fish = {
        "id": "fishs2",
        "models": ["fishaudio/s2-pro"],
        "default_model": "fishaudio/s2-pro",
    }
    _decorate_model_language_support(fish)
    fish_support = fish["model_catalog"][0]["language_support"]
    assert fish_support["coverage"] == "claim"
    assert len(fish_support["languages"]) == 83
    assert "en" in fish_support["languages"]

    known_services = [
        {
            "id": "vertex_ai",
            "models": ["gemini-2.5-flash-tts"],
            "default_model": "gemini-2.5-flash-tts",
        },
    ]
    expected_counts = {"vertex_ai": 87}
    for service in known_services:
        _decorate_model_language_support(service)
        model = service["model_catalog"][0]
        assert model["language_support"]["coverage"] == "exact"
        assert len(model["supported_languages"]) == expected_counts[service["id"]]

    direct_gemini = {
        "id": "gemini",
        "models": ["gemini-2.5-flash-preview-tts"],
    }
    _decorate_model_language_support(direct_gemini)
    direct_support = direct_gemini["model_catalog"][0]["language_support"]
    assert direct_support["coverage"] == "unknown"
    assert direct_support["languages"] == []

    custom_openai_compatible = {
        "id": "my_custom_openai_profile",
        "provider": "openai",
        "adapter": "openai_compatible",
        "models": ["gpt-4o-mini-tts"],
        "default_model": "gpt-4o-mini-tts",
        "voice_metadata": {"voice": {"accent": "English"}},
    }
    _decorate_model_language_support(custom_openai_compatible)
    custom_support = custom_openai_compatible["model_catalog"][0]["language_support"]
    assert custom_support["provider_id"] == "my_custom_openai_profile"
    assert custom_support["coverage"] == "unknown"
    assert custom_support["languages"] == []


def test_native_provider_service_identity_and_compact_projection_exclude_secrets():
    secret = "super-secret-endpoint-key"
    service = {
        "id": "elevenlabs_custom",
        "provider": "elevenlabs",
        "adapter": "elevenlabs_native",
        "api_base": f"https://example.invalid/v1?api_key={secret}",
        "api_key": secret,
        "models": ["eleven_multilingual_v2"],
        "default_model": "eleven_multilingual_v2",
        "model_catalog": [
            {
                "id": "eleven_multilingual_v2",
                "languages": [{"language_id": "en", "api_key": secret}],
                "language_coverage": "exact",
                "discovery": "provider_live",
                "native_route": "elevenlabs_text_to_speech",
                "sources": [f"https://example.invalid/docs?token={secret}"],
                "endpoint": f"https://example.invalid/{secret}",
                "credentials": {"api_key": secret},
            }
        ],
    }

    _decorate_model_language_support(service)
    full = _models_by_id(service)["eleven_multilingual_v2"]["language_support"]
    compact = _slim_model_catalog(service)[0]
    compact_support = compact["language_support"]

    assert full["provider_id"] == "elevenlabs"
    assert full["service_id"] == "elevenlabs_custom"
    assert compact_support["provider_id"] == "elevenlabs"
    assert compact_support["service_id"] == "elevenlabs_custom"
    assert "sources" not in compact_support
    assert "endpoint" not in compact
    assert "credentials" not in compact
    assert secret not in json.dumps(compact.get("languages"))
    assert secret not in json.dumps(full)
    assert secret not in json.dumps(compact)
