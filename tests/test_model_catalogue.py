from __future__ import annotations

import json
from unittest import mock

from pandrator.logic import tts_handler, tts_provider_profiles
from pandrator.logic.audio_cpp_catalogue import inventory, package_metadata
from pandrator.logic.model_catalogue import _catalogue_rows, catalogue_page
from pandrator.logic.speech_performance import capabilities_for_model


def _item(provider_id: str, model_id: str) -> dict:
    page = catalogue_page(provider=provider_id, query=model_id, limit=100)
    return next(row for row in page["items"] if row["id"] == model_id)


def test_catalogue_has_local_cloud_and_static_azure_models():
    page = catalogue_page(limit=1)
    provider_ids = {provider["id"] for provider in page["providers"]}

    assert page["schema_version"] == 1
    assert page["runtime_version"]
    assert {
        "audio_cpp",
        "xtts",
        "openai",
        "gemini",
        "vertex_ai",
        "azure",
        "elevenlabs",
    } <= provider_ids
    assert page["total"] > 1
    assert page["next_offset"] == 1
    assert page["items"][0]["provider_id"] == "audio_cpp"

    audio_model_id = inventory()["packages"][0]["id"]
    audio_item = _item("audio_cpp", audio_model_id)
    source_metadata = package_metadata(audio_model_id)
    assert all(audio_item[key] == value for key, value in source_metadata.items())
    assert audio_item["catalogue_id"] == f"audio_cpp:{audio_model_id}"

    azure = _item("azure", "MAI-Voice-2")
    assert azure["catalogue_id"] == "azure:MAI-Voice-2"
    assert azure["voice_mode"] == "prebuilt"
    assert azure["capabilities"] == ["emotion_control", "prebuilt_voices"]
    assert azure["pandrator_features"]["emotion_control"] == "discrete_styles"
    assert azure["pandrator_features"]["instructions"] == "none"
    assert azure["pandrator_features"]["voice_design"] == "none"
    assert azure["pandrator_features"]["vocal_events"] == "none"
    assert any(
        "azure_speech_style" in note
        and "general pSSML" in note
        and "every voice" in note
        for note in azure["pandrator_features"]["capability_notes"]
    )
    assert azure["upstream_features"] == {"emotion_control": True}
    assert "https://learn.microsoft.com/en-us/azure/ai-services/speech-service/mai-voices" in azure["sources"]
    assert "https://learn.microsoft.com/azure/ai-services/speech-service/mai-voices" in azure["sources"]
    assert _item("azure", "MAI-Voice-2-Flash")["catalogue_id"] == (
        "azure:MAI-Voice-2-Flash"
    )
    assert "en" in _item("xtts", "tts_models/multilingual/multi-dataset/xtts_v2")[
        "supported_languages"
    ]


def test_cloud_capabilities_are_model_and_provider_specific():
    mini = _item("openai", "gpt-4o-mini-tts")
    assert mini["capabilities"] == [
        "emotion_control",
        "instructions",
        "prebuilt_voices",
    ]
    assert mini["upstream_features"] == {
        "instructions": True,
        "emotion_control": True,
    }

    for model_id in ("tts-1", "tts-1-hd"):
        assert _item("openai", model_id)["capabilities"] == ["prebuilt_voices"]

    gemini = _item("gemini", "gemini-2.5-flash-preview-tts")
    vertex = _item("vertex_ai", "gemini-2.5-flash-tts")
    expected = [
        "emotion_control",
        "instructions",
        "prebuilt_voices",
        "vocal_events",
    ]
    assert gemini["capabilities"] == expected
    assert vertex["capabilities"] == expected
    assert gemini["catalogue_id"] == "gemini:gemini-2.5-flash-preview-tts"
    assert vertex["catalogue_id"] == "vertex_ai:gemini-2.5-flash-tts"
    for item, backend in ((gemini, "gemini"), (vertex, "vertex_ai")):
        profile = capabilities_for_model(
            item["id"], backend=backend, family=item["family"], voice_mode=item["voice_mode"]
        )
        assert item["pandrator_features"]["vocal_events"] == ", ".join(
            profile["event_tags"]
        )
        assert item["pandrator_features"]["instruction_scope"] == ", ".join(
            profile["instruction_scope"]
        )
        assert item["pandrator_features"]["semantic_context"] == profile[
            "semantic_context"
        ]
        assert item["pandrator_features"]["capability_notes"] == profile["notes"]
    assert "laugh" in gemini["pandrator_features"]["vocal_events"]
    assert "gasp" in vertex["pandrator_features"]["vocal_events"]
    assert gemini["catalogue_id"] != vertex["catalogue_id"]

    rows = catalogue_page(provider="gemini", limit=100)["items"]
    assert {row["id"] for row in rows if row["provider_id"] == "gemini"} >= set(
        tts_handler.GEMINI_TTS_MODELS
    )
    vertex_rows = catalogue_page(provider="vertex_ai", limit=100)["items"]
    assert {row["id"] for row in vertex_rows} >= set(tts_handler.VERTEX_TTS_MODELS)


def test_elevenlabs_models_distinguish_v3_directions_from_v2_and_setup_state():
    provider_rows = {
        row["id"]: row
        for row in catalogue_page(provider="elevenlabs", limit=100)["items"]
    }
    assert {
        "eleven_v3",
        "eleven_multilingual_v2",
        "eleven_flash_v2_5",
        "eleven_turbo_v2_5",
    } <= set(provider_rows)

    v3 = provider_rows["eleven_v3"]
    v3_profile = capabilities_for_model(
        "eleven_v3", backend="elevenlabs_native", voice_mode="prebuilt"
    )
    assert v3["pandrator_features"]["instructions"] == "inline"
    assert v3["pandrator_features"]["instruction_scope"] == "request, span"
    assert v3["pandrator_features"]["vocal_events"] == ", ".join(
        v3_profile["event_tags"]
    )
    assert v3["pandrator_features"]["capability_notes"] == v3_profile["notes"]

    v2 = provider_rows["eleven_multilingual_v2"]
    assert v2["pandrator_features"]["instructions"] == "none"
    assert v2["pandrator_features"]["instruction_scope"] == "none"
    assert v2["pandrator_features"]["vocal_events"] == "none"
    assert "instructions" not in v2["capabilities"]
    assert "vocal_events" not in v2["capabilities"]

    turbo = provider_rows["eleven_turbo_v2_5"]
    assert turbo["upstream_status"] == "deprecated"
    assert turbo["recommended_for"] == ""
    assert turbo["package_availability"]["status"] == "external_service"
    assert any("eleven_flash_v2_5" in note for note in turbo["pandrator_features"]["capability_notes"])
    for model in provider_rows.values():
        assert model["package_availability"]["status"] == "external_service"
        assert not {"multi_speaker", "sound_generation", "sound_effects"} & set(
            model.get("capabilities", [])
        )


def test_provider_filter_search_and_capability_alias_keep_facets_independent():
    page = catalogue_page(provider="openai", query="OpenAI", limit=100)
    assert page["items"]
    assert {row["provider_id"] for row in page["items"]} == {"openai"}
    assert {row["id"] for row in page["providers"]} >= {"azure", "gemini", "vertex_ai"}

    emotions = catalogue_page(capability="emotion_control", limit=100)
    alias = catalogue_page(capability="emotions", limit=100)
    assert [row["catalogue_id"] for row in emotions["items"]] == [
        row["catalogue_id"] for row in alias["items"]
    ]


def test_permitted_commercial_filter_includes_attribution_permission():
    item = {
        "id": "attributed-model",
        "provider_id": "fixture",
        "provider_name": "Fixture",
        "provider_kind": "local",
        "catalogue_id": "fixture:attributed-model",
        "family": "fixture",
        "family_label": "Fixture",
        "label": "Attributed model",
        "category": "tts",
        "supported_languages": [],
        "capabilities": [],
        "commercial_use": "permitted_with_attribution",
        "recommended_for": "",
    }
    with mock.patch(
        "pandrator.logic.model_catalogue._catalogue_rows",
        return_value=([item], []),
    ):
        page = catalogue_page(commercial_use="permitted")

    assert [row["id"] for row in page["items"]] == ["attributed-model"]


def test_pagination_is_deterministic_and_catalogue_ids_are_unique():
    first = catalogue_page(limit=9, offset=0)
    first_again = catalogue_page(limit=9, offset=0)
    second = catalogue_page(limit=9, offset=9)

    assert first == first_again
    assert first["next_offset"] == 9
    assert second["offset"] == 9
    assert len({row["catalogue_id"] for row in first["items"] + second["items"]}) == 18
    combined = first["items"] + second["items"]

    def sort_key(row: dict) -> tuple:
        return (
            row["provider_name"].casefold(),
            row["family"].casefold(),
            row["label"].casefold(),
            row["id"].casefold(),
            row["id"],
        )

    assert combined == sorted(combined, key=sort_key)


def test_unknown_registered_profile_does_not_inherit_coarse_capability_flags():
    original = tts_provider_profiles.list_tts_provider_profiles
    future_profile = {
        "id": "future-tts-profile",
        "name": "Future TTS API",
        "provider": "future_vendor",
        "adapter": "generic_json",
        "source_url": "https://example.test/future-tts",
        "models": ["future-instruct-tts"],
        "supports_prebuilt_voices": True,
    }
    with mock.patch.object(
        tts_provider_profiles,
        "list_tts_provider_profiles",
        side_effect=lambda: [*original(), future_profile],
    ):
        _catalogue_rows.cache_clear()
        try:
            item = _item("future-tts-profile", "future-instruct-tts")
        finally:
            _catalogue_rows.cache_clear()

    assert item["voice_mode"] == "unknown"
    assert item["capabilities"] == []
    assert item["pandrator_features"]["instructions"] == "none"
    assert "upstream_features" not in item


def test_catalogue_does_not_emit_connection_or_runtime_state():
    page = catalogue_page(limit=100)
    forbidden = {
        "api_key",
        "api_key_env",
        "api_base",
        "base_url",
        "endpoint",
        "secret_ref",
        "settings",
        "vertex_project",
        "vertex_location",
    }

    def check(value):
        if isinstance(value, dict):
            assert not ({str(key).casefold() for key in value} & forbidden)
            for key, nested in value.items():
                if key == "capability_notes":
                    assert isinstance(nested, list)
                    assert all(isinstance(note, str) for note in nested)
                    assert all(
                        not any(
                            marker in note.casefold()
                            for marker in ("http://", "https://", "api_key", "api key", "base_url", "secret")
                        )
                        for note in nested
                    )
                check(nested)
        elif isinstance(value, list):
            for nested in value:
                check(nested)

    check(page)
    json.dumps(page)
