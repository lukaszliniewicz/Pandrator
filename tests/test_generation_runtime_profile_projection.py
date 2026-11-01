"""Pure contracts for selected-model runtime profile projections."""

from copy import deepcopy
from typing import Any

import pytest

from pandrator.logic.speech_performance import compile_performance
from pandrator.web.generation_audio_identity import _material_settings
from pandrator.web.generation_runtime_speech import GenerationSpeechRuntime
from pandrator.web.generation_speech_connections import _connection_fingerprint


class DeepcopyBomb:
    def __deepcopy__(self, memo):
        raise AssertionError("an unselected model body was copied")


def _profile(*, adapter: str = "audio_cpp") -> dict[str, Any]:
    return {
        "id": "fixture-custom-audio",
        "name": "Fixture custom audio",
        "is_custom": True,
        "provider": "audio_cpp",
        "adapter": adapter,
        "api_base": "http://fixture.invalid:8060",
        "api_key_env": "",
        "api_key": "",
        "secret_ref": "",
        "profile_id": "fixture-custom-audio",
        "auth_mode": "none",
        "direct_http": True,
        "credential_required": False,
        "speech_path": "/v1/audio/speech",
        "models": ["fixture-model-a", "fixture-model-b"],
        "default_model": "fixture-model-a",
        "voices": ["Fixture Voice"],
        "default_voice": "Fixture Voice",
        "default_voices": {
            "fixture-model-a": "Fixture Voice",
            "fixture-model-b": "Fixture Voice",
        },
        "voice_catalogues": {
            "fixture-model-a": ["Fixture Voice"],
            "fixture-model-b": ["Fixture Voice"],
        },
        "model_voice_modes": {
            "fixture-model-a": "design",
            "fixture-model-b": "cloning",
        },
        "model_catalog": [
            {
                "id": "fixture-model-a",
                "family": "qwen3_tts",
                "voice_mode": "design",
                "nested": {"choices": ["model-a"]},
            },
            {
                "id": "fixture-model-b",
                "family": "fish_audio_s2",
                "voice_mode": "cloning",
                "nested": {"choices": ["model-b"]},
            },
        ],
        "settings": {"nested": {"values": ["transport-setting"]}},
        "request_defaults": {"format": "wav"},
        "extra_body": {"speaker": "fixture-speaker"},
        "pricing": {"fixture-model-a": {"per_character": 0}},
    }


def _settings(model: str = "fixture-model-a") -> dict[str, Any]:
    return {
        "service": "fixture-custom-audio",
        "model": model,
        "xtts_model": model,
        "language": "auto",
        "voice": "Fixture Voice",
        "audio_cpp_model_settings": {model: {"temperature": 0.7}},
        "external_server_url": "remove-me",
    }


def test_default_canonicalization_keeps_full_profile_and_existing_settings_projection():
    profile = _profile()
    settings = _settings()
    source_before = deepcopy(settings)

    canonical = GenerationSpeechRuntime._canonical(settings, profile)

    assert canonical["provider_configs"][0] is profile
    assert canonical["provider_configs"][0]["model_catalog"] == profile["model_catalog"]
    assert canonical["audio_cpp_base_url"] == profile["api_base"]
    assert "external_server_url" not in canonical
    assert settings == source_before


def test_selected_canonical_profile_owns_ordered_casefold_rows_and_skips_unselected_body():
    profile = _profile()
    selected_first = {
        "id": " Fixture-Model-A ",
        "family": "qwen3_tts",
        "voice_mode": "design",
        "nested": {"choices": ["first"]},
    }
    selected_duplicate = {
        "id": "fixture-model-a",
        "family": "qwen3_tts",
        "voice_mode": "design",
        "nested": {"choices": ["duplicate"]},
    }
    poison = DeepcopyBomb()
    source_catalogue = [
        selected_first,
        {"id": "not-a-dict-evidence"},
        "not a model row",
        selected_duplicate,
        {"id": "fixture-model-b", "payload": poison},
    ]
    profile["model_catalog"] = source_catalogue
    profile_without_catalogue = deepcopy(
        {key: value for key, value in profile.items() if key != "model_catalog"}
    )
    settings = _settings(" FIXTURE-MODEL-A ")
    settings_before = deepcopy(settings)

    canonical = GenerationSpeechRuntime._canonical(
        settings, profile, selected_model=" FIXTURE-MODEL-A "
    )
    projected = canonical["provider_configs"][0]

    assert [row["id"] for row in projected["model_catalog"]] == [
        " Fixture-Model-A ",
        "fixture-model-a",
    ]
    assert projected["model_catalog"][0] is not selected_first
    assert projected["model_catalog"][1] is not selected_duplicate
    assert projected["settings"] is not profile["settings"]
    assert projected["settings"] == profile_without_catalogue["settings"]
    projected["model_catalog"][0]["nested"]["choices"].append("caller mutation")
    projected["settings"]["nested"]["values"].append("caller mutation")
    assert selected_first["nested"]["choices"] == ["first"]
    assert profile["settings"] == profile_without_catalogue["settings"]
    assert profile["model_catalog"] is source_catalogue
    assert source_catalogue[-1]["payload"] is poison
    assert settings == settings_before


@pytest.mark.parametrize(
    ("has_catalogue", "catalogue_value"),
    [(False, None), (True, {"id": "fixture-model-a"}), (True, None)],
)
def test_selected_canonical_profile_preserves_missing_and_nonlist_catalogue_shape(
    has_catalogue: bool, catalogue_value: Any,
):
    profile = _profile()
    profile.pop("model_catalog")
    if has_catalogue:
        profile["model_catalog"] = catalogue_value

    canonical = GenerationSpeechRuntime._canonical(
        _settings(), profile, selected_model="fixture-model-a"
    )
    projected = canonical["provider_configs"][0]

    assert ("model_catalog" in projected) is has_catalogue
    if has_catalogue:
        assert projected["model_catalog"] == catalogue_value
        if isinstance(catalogue_value, dict):
            assert projected["model_catalog"] is not catalogue_value


def test_selected_profile_reuse_preserves_material_compiled_and_transport_identities():
    frozen_profile = _profile()
    runtime_profile = deepcopy(frozen_profile)
    frozen_before = deepcopy(frozen_profile)
    runtime_before = deepcopy(runtime_profile)

    full_settings = GenerationSpeechRuntime._canonical(_settings(), frozen_profile)
    selected_a_settings = GenerationSpeechRuntime._canonical(
        _settings("fixture-model-a"),
        frozen_profile,
        selected_model="fixture-model-a",
    )
    selected_b_settings = GenerationSpeechRuntime._canonical(
        _settings("fixture-model-b"),
        runtime_profile,
        selected_model="fixture-model-b",
    )
    full_projected_profile = full_settings["provider_configs"][0]
    selected_a_profile = selected_a_settings["provider_configs"][0]
    selected_b_profile = selected_b_settings["provider_configs"][0]

    assert [row["id"] for row in full_projected_profile["model_catalog"]] == [
        "fixture-model-a",
        "fixture-model-b",
    ]
    assert [(row["family"], row["voice_mode"]) for row in selected_a_profile["model_catalog"]] == [
        ("qwen3_tts", "design"),
    ]
    assert [(row["family"], row["voice_mode"]) for row in selected_b_profile["model_catalog"]] == [
        ("fish_audio_s2", "cloning"),
    ]
    assert len(frozen_profile["model_catalog"]) == 2
    assert len(runtime_profile["model_catalog"]) == 2

    for model, full_source, selected_settings, selected_profile in (
        (
            "fixture-model-a",
            frozen_profile,
            selected_a_settings,
            selected_a_profile,
        ),
        (
            "fixture-model-b",
            runtime_profile,
            selected_b_settings,
            selected_b_profile,
        ),
    ):
        full_working_settings = GenerationSpeechRuntime._canonical(
            _settings(model), full_source
        )
        full_profile = full_working_settings["provider_configs"][0]
        full_material = _material_settings(
            {"tts": full_working_settings, "audio": {}}, {}
        )
        selected_material = _material_settings(
            {"tts": selected_settings, "audio": {}}, {}
        )
        assert full_material == selected_material

        full_compiled = compile_performance(
            "A short fixture sentence.",
            full_working_settings,
            endpoint=full_profile,
        )
        selected_compiled = compile_performance(
            "A short fixture sentence.",
            selected_settings,
            endpoint=selected_profile,
        )
        assert full_compiled.fingerprint == selected_compiled.fingerprint
        assert _connection_fingerprint(full_profile) == _connection_fingerprint(
            selected_profile
        )

    assert frozen_profile == frozen_before
    assert runtime_profile == runtime_before


def test_selected_azure_alias_keeps_canonical_model_metadata():
    from pandrator.logic.azure_speech_emotions import canonical_azure_speech_model

    alias = "MAI-Voice-2"
    canonical_model = canonical_azure_speech_model(alias)
    assert canonical_model == "MAI-Voice-2.1"
    profile = _profile(adapter="azure_speech")
    profile["model_catalog"] = [
        {"id": alias, "source": "legacy-alias"},
        {"id": canonical_model, "source": "canonical"},
        {"id": "other-model", "source": "unselected"},
    ]
    source_before = deepcopy(profile)

    canonical = GenerationSpeechRuntime._canonical(
        _settings(alias), profile, selected_model=alias
    )
    projected = canonical["provider_configs"][0]

    assert [row["id"] for row in projected["model_catalog"]] == [
        alias,
        canonical_model,
    ]
    assert profile == source_before
