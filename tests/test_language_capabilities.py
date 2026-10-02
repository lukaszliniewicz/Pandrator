from __future__ import annotations

import pytest

from pandrator.logic.language_capabilities import (
    canonical_language_tag,
    language_decision,
    language_matches,
    registry_snapshot,
    require_supported_language,
    source_language_record,
    support_record,
)


def test_canonical_language_tags_preserve_locales_and_distinct_languages():
    assert canonical_language_tag(" PT_br ") == "pt-br"
    assert canonical_language_tag("uk-UA") == "uk-ua"
    assert canonical_language_tag("no") == "no"
    assert canonical_language_tag("nb") == "nb"
    assert canonical_language_tag("nn") == "nn"
    assert canonical_language_tag("tl") == "tl"
    assert canonical_language_tag("fil") == "fil"
    assert canonical_language_tag("qzz-Latn") == "qzz-latn"
    assert canonical_language_tag("Javanese") == "jv"


def test_automatic_values_require_explicit_opt_in_and_prose_is_rejected():
    assert canonical_language_tag("Detect", allow_auto=True) == "auto"
    assert canonical_language_tag("und", allow_auto=True) == "auto"
    with pytest.raises(ValueError, match="Automatic language"):
        canonical_language_tag("automatic")
    with pytest.raises(ValueError, match="Invalid language"):
        canonical_language_tag("the language is English")
    with pytest.raises(ValueError, match="Invalid language"):
        canonical_language_tag("en--US")


def test_language_matches_base_tag_but_not_distinct_locales():
    assert language_matches("en", "en-GB")
    assert language_matches("en-GB", "en")
    assert language_matches("en-GB", "en-GB")
    assert not language_matches("en-GB", "en-US")
    assert not language_matches("", "en")
    assert not language_matches("auto", "en")


def test_registry_snapshot_is_a_detached_copy():
    first = registry_snapshot()
    initial_label = first["languages"][0]["label"]
    first["languages"][0]["label"] = "mutated caller value"
    first["sources"].clear()

    second = registry_snapshot()
    assert second["languages"][0]["label"] == initial_label
    assert second["sources"]


def test_fish_and_omni_records_retain_pinned_source_provenance_and_aliases():
    fish = source_language_record("fish_s2_pro83")
    omni = source_language_record("omnivoice646")

    assert fish["coverage"] == "claim"
    assert len(fish["languages"]) == 83
    assert fish["request_aliases"] == {"jv": "jw"}
    assert len(omni["languages"]) == 646
    assert fish["revision"].startswith("Fish-S2-Pro@")
    assert omni["revision"].startswith("OmniVoice@")
    fish_languages = sorted(set(fish["languages"]))
    fish["languages"].clear()
    assert len(source_language_record("fish_s2_pro83")["languages"]) == 83

    record = support_record(
        provider_id="test-provider",
        model_id="fish-s2-pro",
        model_revision="runtime-model-revision",
        operation="tts",
        native_route="/audio/speech",
        source_key="fish_s2_pro83",
        source_urls=("https://provider.example/models/fish-s2-pro",),
    )
    assert record["schema_version"] == 1
    assert record["catalogue_revision"] == registry_snapshot()["catalogue_revision"]
    assert record["model_revision"] == "runtime-model-revision"
    assert record["source_revision"] == fish["revision"]
    assert record["languages"] == fish_languages
    assert record["request_aliases"] == {"jv": "jw"}
    pinned = next(source for source in record["sources"] if source.get("id") == "fish_s2_pro_readme")
    assert {"id", "url", "revision", "retrieved_on", "sha256"} <= pinned.keys()
    provider_source = next(
        source for source in record["sources"] if source.get("url") == "https://provider.example/models/fish-s2-pro"
    )
    assert provider_source["source_type"] == "provider_metadata"


def test_silero_native_codes_and_request_aliases_preserve_provider_tokens():
    record = support_record(
        provider_id="silero",
        model_id="silero-v5-cis-base",
        model_revision="app-model-revision",
        operation="asr",
        native_route="transcribe",
        source_key="silero_v5_cis_base",
    )
    assert record["native_language_codes"] == source_language_record("silero_v5_cis_base")[
        "native_language_codes"
    ]
    assert record["request_aliases"]["uk"] == "ukr"
    assert record["source_revision"] != record["model_revision"]


def test_support_decision_distinguishes_exact_subset_unknown_and_independent():
    assert language_decision({"coverage": "exact", "languages": ["en"]}, "en-GB") == "supported"
    assert language_decision({"coverage": "exact", "languages": ["en"]}, "fr") == "unsupported"
    assert language_decision({"coverage": "subset", "languages": ["en"]}, "fr") == "unverified"
    assert language_decision({"coverage": "unknown", "languages": []}, "fr") == "unverified"
    assert language_decision({"coverage": "independent", "languages": []}, "auto") == "independent"
    assert language_decision({"coverage": "unknown", "languages": []}, "auto") == "unverified"

    subset = support_record(
        provider_id="dots",
        model_id="dots-tts",
        model_revision="rev-a",
        operation="tts",
        native_route="synthesize",
        source_key="dots_tts24",
    )
    assert language_decision(subset, "af") == "unverified"


def test_require_supported_language_rejects_only_exact_known_absence():
    exact = {
        "provider_id": "provider-x",
        "model_id": "model-y",
        "operation": "transcribe",
        "coverage": "exact",
        "languages": ["en"],
    }
    with pytest.raises(ValueError, match="provider-x model model-y.*transcribe"):
        require_supported_language(exact, "fr")
    assert require_supported_language({**exact, "coverage": "claim"}, "fr") == "fr"
    assert require_supported_language(exact, "EN_gb") == "en-gb"


def test_support_record_normalizes_language_and_alias_keys_without_losing_tokens():
    record = support_record(
        provider_id="provider-x",
        model_id="model-y",
        model_revision="rev-a",
        operation="tts",
        native_route="synthesize",
        languages=("pt_BR", "en", "pt-br"),
        coverage="exact",
        request_aliases={"UK": "UKR", "pt_BR": "PtBR"},
        source_urls=("https://provider.example/capabilities",),
    )
    assert record["languages"] == ["en", "pt-br"]
    assert record["request_aliases"] == {"pt-br": "PtBR", "uk": "UKR"}
    assert record["native_language_codes"] == []

    with pytest.raises(ValueError, match="conflicting values"):
        support_record(
            provider_id="provider-x",
            model_id="model-y",
            model_revision="rev-a",
            operation="tts",
            native_route="synthesize",
            request_aliases={"pt_BR": "ptbr", "pt-br": "PT-BR"},
        )
    with pytest.raises(ValueError, match="Invalid language"):
        support_record(
            provider_id="provider-x",
            model_id="model-y",
            model_revision="rev-a",
            operation="tts",
            native_route="synthesize",
            languages=("English language",),
        )


def test_unknown_records_are_conservative_and_language_independent_records_empty():
    unknown = support_record(
        provider_id="provider-x",
        model_id="unknown-model",
        model_revision="rev-a",
        operation="tts",
        native_route="synthesize",
    )
    assert unknown["coverage"] == "unknown"
    assert unknown["languages"] == []
    assert language_decision(unknown, "en") == "unverified"
    assert source_language_record("missing-record") == {}

    independent = support_record(
        provider_id="provider-x",
        model_id="language-independent",
        model_revision="rev-a",
        operation="normalize",
        native_route="normalize",
        coverage="independent",
    )
    assert independent["languages"] == []
    assert language_decision(independent, "en") == "independent"

    independent_override = support_record(
        provider_id="provider-x",
        model_id="language-independent",
        model_revision="rev-b",
        operation="normalize",
        native_route="normalize",
        source_key="dots_tts24",
        coverage="independent",
    )
    assert independent_override["languages"] == []
