from __future__ import annotations

from copy import deepcopy

from pandrator.logic import tts_handler, tts_provider_profiles
from pandrator.logic.model_catalogue import _catalogue_rows, catalogue_page

_EXCLUDED_REQUEST_IDENTITIES = {
    ("kobold_qwen", "Prebuilt Voices"),
    ("kobold_qwen", "Voice Cloning"),
    ("voxtral", "auto"),
    ("voxtral", "gguf"),
    ("voxtral", "bf16"),
    ("pandrator-voxtral-fastapi", "auto"),
    ("pandrator-voxtral-fastapi", "gguf"),
    ("pandrator-voxtral-fastapi", "bf16"),
    ("pandrator-kobold-qwen-fastapi", "qwen3-tts"),
    ("qwen3-second-state", "qwen3-tts"),
    ("chatterbox-travisvn", "chatterbox-tts-1"),
    ("cosyvoice-jianchang512", "tts-1"),
    ("open-unified-tts", "tts-1"),
    ("kokoro", "tts-1"),
    ("kokoro", "tts-1-hd"),
    ("kokoro", "gpt-4o-mini-tts"),
}

_ALIASES = {
    ("voxcpm", "openbmb/VoxCPM2"): ("voxcpm2",),
    ("pandrator-voxcpm-fastapi", "openbmb/VoxCPM2"): ("voxcpm2",),
    ("pandrator-fishs2-fastapi", "fishaudio/s2-pro"): (
        "fishs2",
        "fish-s2",
        "s2-pro",
    ),
}


def _identities() -> set[tuple[str, str]]:
    rows, _ = _catalogue_rows()
    return {(row["provider_id"], row["id"]) for row in rows}


def test_exact_request_selectors_are_absent_without_removing_real_model_ids():
    identities = _identities()

    assert _EXCLUDED_REQUEST_IDENTITIES.isdisjoint(identities)
    assert {
        ("openai", "tts-1"),
        ("openai", "tts-1-hd"),
        ("openai", "gpt-4o-mini-tts"),
        ("gemini", "gemini-2.5-flash-preview-tts"),
        ("vertex_ai", "gemini-2.5-flash-tts"),
    } <= identities


def test_reviewed_aliases_project_to_one_canonical_row_per_provider():
    rows, _ = _catalogue_rows()
    identities = {(row["provider_id"], row["id"]) for row in rows}

    for (provider_id, canonical_id), aliases in _ALIASES.items():
        matches = [
            row
            for row in rows
            if row["provider_id"] == provider_id and row["id"] == canonical_id
        ]
        assert len(matches) == 1
        assert matches[0]["entry_role"] == "model"
        assert matches[0]["request_aliases"] == sorted(aliases)
        assert all((provider_id, alias) not in identities for alias in aliases)


def test_family_labelled_models_and_named_chatterbox_variants_remain():
    identities = _identities()

    assert {
        ("kokoro", "kokoro"),
        ("audio_cpp", "piper_lessac_medium_orig"),
        ("styletts2-salad", "styletts2"),
        ("matcha-projecte-aina", "matcha-tts"),
        ("pandrator-fishs2-fastapi", "fishaudio/s2-pro"),
        ("chatterbox", "chatterbox-en"),
        ("chatterbox", "chatterbox-multilingual"),
        ("chatterbox", "chatterbox-turbo"),
    } <= identities


def test_search_matches_each_reviewed_request_alias_to_its_canonical_row():
    for (provider_id, canonical_id), aliases in _ALIASES.items():
        for alias in aliases:
            page = catalogue_page(provider=provider_id, query=alias, limit=100)
            assert any(
                row["id"] == canonical_id and row["provider_id"] == provider_id
                for row in page["items"]
            )


def test_catalogue_projection_does_not_mutate_defaults_or_provider_profiles():
    defaults_before = deepcopy(tts_handler._default_service_configs())
    profiles_before = deepcopy(tts_provider_profiles.list_tts_provider_profiles())
    _catalogue_rows.cache_clear()

    try:
        _catalogue_rows()
        assert tts_handler._default_service_configs() == defaults_before
        assert tts_provider_profiles.list_tts_provider_profiles() == profiles_before
    finally:
        _catalogue_rows.cache_clear()


def test_mutating_returned_page_does_not_poison_catalogue_cache():
    first_page = catalogue_page(provider="voxcpm", query="voxcpm2", limit=100)
    first_item = next(row for row in first_page["items"] if row["id"] == "openbmb/VoxCPM2")
    provider_name = next(
        provider["name"] for provider in first_page["providers"] if provider["id"] == "voxcpm"
    )

    first_item["request_aliases"].append("poisoned-alias")
    first_item["supported_languages"].append("x-poisoned")
    next(provider for provider in first_page["providers"] if provider["id"] == "voxcpm")[
        "name"
    ] = "Poisoned Provider"

    second_page = catalogue_page(provider="voxcpm", query="voxcpm2", limit=100)
    second_item = next(row for row in second_page["items"] if row["id"] == "openbmb/VoxCPM2")

    assert second_item["request_aliases"] == ["voxcpm2"]
    assert "x-poisoned" not in second_item["supported_languages"]
    assert next(
        provider["name"] for provider in second_page["providers"] if provider["id"] == "voxcpm"
    ) == provider_name
