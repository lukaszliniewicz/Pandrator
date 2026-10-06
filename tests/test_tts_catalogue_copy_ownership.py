"""Catalogue copies preserve values while isolating mutable ownership."""

from copy import deepcopy
from datetime import date

import pytest

from pandrator.logic import tts_service_catalogue as catalogue


class FixedDate(date):
    @classmethod
    def today(cls):
        return cls(2026, 10, 7)


@pytest.fixture
def native_constants(monkeypatch):
    models = [
        {
            "id": "qwen3_tts_1_7b_base_q8_0", "family": "qwen3_tts",
            "voice_mode": "cloning", "nested": {"values": ["default"]},
        },
        {
            "id": "breeze_tts_2_q8_0", "family": "breeze_tts",
            "voice_mode": "optional_cloning", "nested": {"values": ["breeze"]},
        },
    ]
    modes = {row["id"]: row["voice_mode"] for row in models}
    monkeypatch.setattr(catalogue, "AUDIO_CPP_MODEL_CATALOG", models)
    monkeypatch.setattr(catalogue, "AUDIO_CPP_MODEL_VOICE_MODES", modes)
    monkeypatch.setattr(catalogue, "date", FixedDate)
    return models, modes


def configured_native_settings():
    return {"provider_configs": [{
        "id": "audio_cpp", "api_base": "http://native.invalid:8060", "adapter": "audio_cpp",
        "models": ["fixture_native"], "default_model": "fixture_native",
        "model_catalog": [{
            "id": "fixture_native", "family": "breeze_tts", "voice_mode": "optional_cloning",
            "nested": {"values": ["configured"]},
        }],
        "model_voice_modes": {"fixture_native": "optional_cloning"},
        "voices": ["fixture_voice"], "voice_catalogues": {"fixture_native": ["fixture_voice"]},
        "voice_metadata": {"fixture_voice": {"profile": {"tags": ["baseline"]}}},
        "request_defaults": {"sampling": {"seed": [1, 2]}},
    }]}


def test_default_factory_owns_native_globals_and_other_nested_defaults(native_constants):
    models, modes = native_constants
    original_models, original_modes = deepcopy(models), deepcopy(modes)
    original_prices = deepcopy(catalogue.DEFAULT_TTS_PRICING)
    first = {record["id"]: record for record in catalogue._default_service_configs()}
    second = {record["id"]: record for record in catalogue._default_service_configs()}
    expected = deepcopy(second)
    first["audio_cpp"]["model_catalog"][0]["nested"]["values"].append("changed")
    first["audio_cpp"]["model_voice_modes"][models[0]["id"]] = "changed"
    first["openai"]["pricing"]["tts-1"]["input_cost_per_million_characters"] = -1
    first["kokoro"]["voices"].append("changed")
    assert models == original_models
    assert modes == original_modes
    assert catalogue.DEFAULT_TTS_PRICING == original_prices
    assert second == expected
    assert {record["id"]: record for record in catalogue._default_service_configs()} == expected


@pytest.mark.parametrize("use_cache", [False, True])
def test_public_catalogues_and_selected_records_isolate_inputs_and_cache(native_constants, use_cache):
    models, modes = native_constants
    globals_before = deepcopy((models, modes))
    settings = configured_native_settings()
    inputs_before = deepcopy(settings)
    request_cache = {} if use_cache else None
    public = catalogue.get_service_configs(settings, request_cache)
    selected = catalogue.get_service_config(settings, "audio_cpp", request_cache)
    baseline = deepcopy(selected)
    cached_before = deepcopy(request_cache)
    native = next(record for record in public if record["id"] == "audio_cpp")
    native["model_catalog"][0]["nested"]["values"].append("public response")
    native["voice_metadata"]["fixture_voice"]["profile"]["tags"].append("public response")
    selected["request_defaults"]["sampling"]["seed"].append(99)
    selected["model_voice_modes"]["fixture_native"] = "changed"
    fresh = catalogue.get_service_config(settings, "audio_cpp", request_cache)
    assert fresh == baseline
    assert next(
        record for record in catalogue.get_service_configs(settings, request_cache)
        if record["id"] == "audio_cpp"
    ) == baseline
    assert settings == inputs_before
    assert request_cache == cached_before
    assert (models, modes) == globals_before
    settings["provider_configs"][0]["model_catalog"][0]["nested"]["values"].append("caller")
    assert fresh == baseline


@pytest.mark.parametrize("replacement", [[], {}, [{"nested": ["new"]}], {"nested": ["new"]}])
def test_merge_model_catalog_replacement_is_independent_of_base_and_raw(replacement):
    base = {
        "id": "audio_cpp", "model_catalog": [{"nested": ["old"]}],
        "settings": {"nested": ["keep"]}, "models": ["old"],
    }
    raw = {"model_catalog": deepcopy(replacement)}
    base_before, raw_before = deepcopy(base), deepcopy(raw)
    merged = catalogue._merge_service_config(base, raw)
    assert merged["model_catalog"] == replacement
    if isinstance(merged["model_catalog"], list):
        merged["model_catalog"].append({"nested": ["response"]})
    else:
        merged["model_catalog"]["response"] = ["changed"]
    merged["settings"]["nested"].append("response")
    assert base == base_before
    assert raw == raw_before


@pytest.mark.parametrize("raw", [{}, {"model_catalog": None}, {"model_catalog": "wrong"}, {"model_catalog": 7}, {"model_catalog": ()}])
def test_merge_wrong_type_or_absent_model_catalog_retains_owned_base(raw):
    base = {"id": "audio_cpp", "model_catalog": [{"nested": ["old"]}], "models": ["old"]}
    expected = deepcopy(base)
    merged = catalogue._merge_service_config(base, raw)
    assert merged["model_catalog"] == expected["model_catalog"]
    merged["model_catalog"][0]["nested"].append("response")
    assert base == expected


def test_fresh_request_catalogues_keep_date_derived_pricing_fresh(monkeypatch):
    class MovingDate(date):
        current = date(2026, 12, 31)

        @classmethod
        def today(cls):
            return cls.current

    monkeypatch.setattr(catalogue, "date", MovingDate)
    first_cache = {}
    promotional = catalogue.get_service_config({}, "gemini", first_cache)
    MovingDate.current = date(2027, 1, 1)
    regular = catalogue.get_service_config({}, "gemini", {})
    assert promotional["pricing"]["gemini-3.8-flash-tts"]["input_cost_per_million_tokens"] == 0.5
    assert regular["pricing"]["gemini-3.8-flash-tts"]["input_cost_per_million_tokens"] == 1.0
    assert catalogue.get_service_config({}, "gemini", first_cache) == promotional
