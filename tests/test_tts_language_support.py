from __future__ import annotations

from pandrator.logic.language_capabilities import language_decision
from pandrator.logic.model_catalogue import _build_item, catalogue_page
from pandrator.logic.tts_language_support import tts_language_support


def _item(provider_id: str, model_id: str) -> dict:
    page = catalogue_page(provider=provider_id, query=model_id, limit=100)
    return next(row for row in page["items"] if row["id"] == model_id)


def test_fish_silero_and_vertex_use_source_records_and_stable_routes():
    fish = tts_language_support("fishs2", "fishaudio/s2-pro")
    assert fish["coverage"] == "claim"
    assert len(fish["languages"]) == 83
    assert fish["source_key"] == "fish_s2_pro83"
    assert fish["model_revision"] == "fishaudio/s2-pro"
    assert fish["source_revision"].startswith("Fish-S2-Pro@")
    assert fish["native_route"] == "fishs2_tts"
    assert language_decision(fish, "en") == "supported"
    assert language_decision(fish, "qzz") == "unverified"

    en_indic = tts_language_support("silero", "v3_en_indic")
    indic = tts_language_support("silero", "v3_indic")
    assert en_indic["languages"] == ["en-in"]
    assert en_indic["request_aliases"]["en-in"] == "en-in"
    assert indic["languages"] == ["bn", "gu", "hi", "kn", "ml", "mni", "raj", "ta", "te"]

    vertex = tts_language_support("vertex_ai", "gemini-2.5-flash-tts")
    assert vertex["coverage"] == "exact"
    assert len(vertex["languages"]) == 87
    assert vertex["source_key"] == "vertex_gemini_tts87"
    assert vertex["native_route"] == "vertex_generate_content"


def test_vertex_language_matrix_is_not_borrowed_by_direct_gemini_models():
    direct_gemini = tts_language_support("gemini", "gemini-2.5-flash-preview-tts")
    vertex = tts_language_support("vertex_ai", "gemini-2.5-flash-tts")

    assert direct_gemini["coverage"] == "subset"
    assert direct_gemini["source_key"] == "gemini25_tts_languages"
    assert direct_gemini["source_ids"] == ["gemini_legacy_tts_languages"]
    assert language_decision(direct_gemini, "en") == "supported"
    assert language_decision(direct_gemini, "af") == "unverified"
    assert direct_gemini["native_route"] == "gemini_generate_content"
    assert vertex["languages"]


def test_legacy_gemini_subsets_are_specific_to_model_and_provider():
    expected = {
        "gemini-2.5-flash-preview-tts": "gemini25_tts_languages",
        "gemini-2.5-pro-preview-tts": "gemini25_tts_languages",
        "gemini-3.1-flash-tts-preview": "gemini31_tts_languages",
    }
    for model_id, source_key in expected.items():
        record = tts_language_support("gemini", model_id)
        assert record["coverage"] == "subset"
        assert record["source_key"] == source_key
        assert record["source_ids"] == ["gemini_legacy_tts_languages"]
        assert language_decision(record, "en") == "supported"
        assert language_decision(record, "qzz") == "unverified"
        assert language_decision(record, "af") == (
            "supported" if source_key == "gemini31_tts_languages" else "unverified"
        )
        assert record["native_route"] == "gemini_generate_content"

    gemini31 = tts_language_support("gemini", "gemini-3.1-flash-tts-preview")
    assert "zh" in gemini31["languages"]
    assert "cmn" not in gemini31["languages"]
    assert "cmn" in gemini31["native_language_codes"]

    for provider_id, model_id in [
        ("gemini", "future-model"),
        ("gemini", "gemini-2.5-flash-tts"),
        ("unknown-provider", "gemini-3.1-flash-tts-preview"),
    ]:
        record = tts_language_support(provider_id, model_id)
        assert record["coverage"] == "unknown"
        assert record["languages"] == []
        assert record["source_key"] == ""


def test_legacy_gemini_models_survive_catalogue_english_filter():
    legacy_ids = {
        "gemini-2.5-flash-preview-tts",
        "gemini-2.5-pro-preview-tts",
        "gemini-3.1-flash-tts-preview",
    }
    english = catalogue_page(provider="gemini", language="en", limit=100)
    english_ids = {row["id"] for row in english["items"]}
    assert legacy_ids <= english_ids
    assert "gemini-3.8-flash-tts" in english_ids

    afrikaans = catalogue_page(provider="gemini", language="af", limit=100)
    assert {row["id"] for row in afrikaans["items"]} == {
        "gemini-3.1-flash-tts-preview",
        "gemini-3.8-flash-tts",
    }


def test_static_source_map_is_exact_to_provider_and_model_identity():
    expected = {
        ("openai", "tts-1"): "openai_tts57",
        ("openai", "tts-1-hd"): "openai_tts57",
        ("openai", "gpt-4o-mini-tts"): "openai_tts57",
        ("vertex_ai", "gemini-3.1-flash-tts-preview"): "vertex_gemini_tts87",
        ("vertex_ai", "gemini-2.5-flash-tts"): "vertex_gemini_tts87",
        ("vertex_ai", "gemini-2.5-pro-tts"): "vertex_gemini_tts87",
        ("silero", "v3_de"): "silero_v3_de",
        ("silero", "v3_en"): "silero_v3_en",
        ("silero", "v3_en_indic"): "silero_v3_en_indic",
        ("silero", "v3_es"): "silero_v3_es",
        ("silero", "v3_fr"): "silero_v3_fr",
        ("silero", "v3_indic"): "silero_v3_indic",
        ("silero", "v5_5_ru"): "silero_v5_5_ru",
        ("silero", "v5_cis_base"): "silero_v5_cis_base",
        ("silero", "v5_cis_base_nostress"): "silero_v5_cis_base_nostress",
        ("silero", "v5_cis_ext"): "silero_v5_cis_ext",
        ("elevenlabs", "eleven_multilingual_v2"): "eleven_multilingual_v2_29",
        ("elevenlabs", "eleven_flash_v2_5"): "eleven_multilingual_v2_29",
        ("elevenlabs", "eleven_turbo_v2_5"): "eleven_multilingual_v2_29",
    }

    for (provider_id, model_id), source_key in expected.items():
        record = tts_language_support(provider_id, model_id)
        assert record["source_key"] == source_key
        assert record["coverage"] == "exact"


def test_openai_and_kokoro_compatibility_ids_have_provider_specific_languages():
    openai = tts_language_support("openai", "gpt-4o-mini-tts")
    kokoro_alias = tts_language_support("kokoro", "gpt-4o-mini-tts")
    kokoro_tts1 = tts_language_support("kokoro", "tts-1")

    assert openai["coverage"] == "exact"
    assert len(openai["languages"]) == 57
    assert openai["source_key"] == "openai_tts57"
    assert kokoro_alias["coverage"] == "claim"
    assert "en-gb" in kokoro_alias["languages"]
    assert kokoro_alias["source_key"] == ""
    assert kokoro_tts1["languages"] == kokoro_alias["languages"]
    assert "runtime adapter parity" in kokoro_alias["note"]


def test_elevenlabs_flash_and_turbo_have_the_published_wider_locale_set():
    multilingual = tts_language_support("elevenlabs", "eleven_multilingual_v2")
    flash = tts_language_support("elevenlabs", "eleven_flash_v2_5")
    turbo = tts_language_support("elevenlabs", "eleven_turbo_v2_5")

    assert len(multilingual["languages"]) == 29
    assert len(flash["languages"]) == 32
    assert len(turbo["languages"]) == 32
    assert {"hu", "no", "vi"} <= set(flash["languages"])
    assert flash["source_key"] == turbo["source_key"] == "eleven_multilingual_v2_29"
    assert any(
        source.get("url") == "https://elevenlabs.io/docs/overview/models"
        for source in flash["sources"]
    )


def test_legacy_local_model_lists_are_whitelisted_and_unknown_models_stay_unknown():
    local = tts_language_support("xtts", "tts_models/multilingual/multi-dataset/xtts_v2")
    unknown_xtts = tts_language_support("xtts", "future-model")
    unknown_custom = tts_language_support("my-custom-provider", "gpt-4o-mini-tts")

    assert local["languages"]
    assert local["coverage"] == "claim"
    assert "runtime adapter parity" in local["note"]
    assert unknown_xtts["coverage"] == "unknown"
    assert unknown_xtts["languages"] == []
    assert unknown_custom["coverage"] == "unknown"
    assert unknown_custom["languages"] == []


def test_azure_voice_locales_are_subsets_and_metadata_sources_are_sanitized():
    metadata = {
        "voice_metadata": {
            "voice-en": {"locale": "en-US", "source": "https://docs.example.com/voices"},
            "voice-fr": {"locale": "fr-FR"},
        },
        "sources": [
            "https://docs.example.com/models?view=all",
            "https://user:password@docs.example.com/private",
            "https://api.example.com/v1/audio/speech",
            "http://127.0.0.1:8042/v1/models",
        ],
        "api_base": "https://api.example.com/v1?api_key=not-for-output",
    }
    record = tts_language_support(
        "azure",
        "MAI-Voice-2",
        adapter="azure_speech",
        metadata=metadata,
    )

    assert record["coverage"] == "subset"
    assert record["languages"] == ["en-us", "fr-fr"]
    assert record["native_route"] == "azure_speech"
    assert record["sources"] == [
        {
            "url": "https://docs.example.com/models",
            "source_type": "provider_metadata",
        }
    ]
    assert "api_base" not in record


def test_explicit_live_language_metadata_requires_exact_coverage_and_filters_prose():
    live_exact = tts_language_support(
        "custom-provider",
        "live-model",
        discovery="provider_live",
        metadata={
            "languages": [{"language_id": "pt_BR"}, {"name": "English"}],
            "language_coverage": "exact",
            "model_revision": "runtime-42",
            "revision": "card-17",
            "weight_manifest": {"revision": "weights-3"},
        },
    )
    live_claim = tts_language_support(
        "custom-provider",
        "claim-model",
        discovery="provider_live",
        metadata={
            "languages": ["en", "Many languages including English"],
            "language_coverage": "exact",
        },
    )
    prose_only = tts_language_support(
        "custom-provider",
        "prose-model",
        metadata={"supported_languages": ["English and several more"]},
    )

    assert live_exact["coverage"] == "exact"
    assert live_exact["languages"] == ["en", "pt-br"]
    assert live_exact["model_revision"] == "runtime-42"
    assert live_exact["source_revision"] is None
    assert live_claim["coverage"] == "claim"
    assert live_claim["languages"] == ["en"]
    assert "ignored" in live_claim["note"]
    assert prose_only["coverage"] == "unknown"
    assert prose_only["languages"] == []
    assert "ignored" in prose_only["note"]


def test_weight_manifest_revision_is_used_but_card_revision_is_not():
    record = tts_language_support(
        "custom-provider",
        "model-name",
        metadata={
            "revision": "card-revision",
            "weight_manifest": {"revision": "weights-revision"},
        },
    )
    assert record["model_revision"] == "weights-revision"

    fallback = tts_language_support(
        "custom-provider",
        "model-name",
        metadata={"revision": "card-revision"},
    )
    assert fallback["model_revision"] == "model-name"


def test_model_catalogue_exposes_support_record_and_does_not_use_provider_wide_fallback():
    fish = _item("fishs2", "fishaudio/s2-pro")
    direct_gemini = _item("gemini", "gemini-2.5-flash-preview-tts")
    vertex = _item("vertex_ai", "gemini-2.5-flash-tts")
    kokoro_alias = _item("kokoro", "gpt-4o-mini-tts")
    azure = _item("azure", "MAI-Voice-2")

    assert fish["language_support"]["coverage"] == "claim"
    assert fish["supported_languages"] == fish["language_support"]["languages"]
    assert direct_gemini["language_support"]["coverage"] == "subset"
    assert "en-us" in direct_gemini["supported_languages"]
    assert direct_gemini["language_support"]["source_ids"] == [
        "gemini_legacy_tts_languages"
    ]
    assert vertex["language_support"]["native_route"] == "vertex_generate_content"
    assert kokoro_alias["language_support"]["coverage"] == "claim"
    assert "en-gb" in kokoro_alias["supported_languages"]
    assert azure["language_support"]["coverage"] == "subset"

    custom = _build_item(
        provider_id="custom-profile",
        provider_name="Custom profile",
        provider_kind="custom",
        model_id="unlisted-model",
        model_metadata={"supported_languages": []},
        adapter="generic_json",
    )
    assert custom["language_support"]["coverage"] == "unknown"
    assert custom["supported_languages"] == []


def test_native_route_does_not_leak_an_endpoint_address():
    record = tts_language_support(
        "custom-provider",
        "model-name",
        native_route="https://user:password@internal.example/v1/audio?api_key=hidden",
    )
    assert record["native_route"] == "provider:custom-provider"
    assert "password" not in str(record)
    assert "api_key" not in str(record)
