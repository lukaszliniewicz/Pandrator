"""Sparse provider lookups preserve full-lookup values and ownership."""

import json
from copy import deepcopy
from typing import Any, cast

import pytest

from pandrator.logic import speech_performance, tts_handler
from pandrator.logic import tts_service_catalogue as catalogue
from pandrator.logic.speech_performance import compile_performance, resolve_capabilities
from pandrator.logic.tts_language_preflight import resolve_tts_language_support
from pandrator.web.generation_audio_identity import _material_settings
from pandrator.web.settings_policy import adapt_runtime_settings


def native_settings(
    *, model_catalog: list[Any] | None = None,
    model_voice_modes: dict[str, str] | None = None,
) -> dict[str, Any]:
    if model_catalog is None:
        model_catalog = [{
            "id": "fixture_breeze",
            "family": "qwen3_tts",
            "voice_mode": "design",
            "nested": {"choices": ["configured"]},
        }]
    if model_voice_modes is None:
        model_voice_modes = {"fixture_breeze": "design"}
    return {
        "provider_configs": [{
            "id": "audio_cpp",
            "adapter": "audio_cpp",
            "api_base": "http://native.invalid:8060",
            "models": ["fixture_breeze"],
            "default_model": "fixture_breeze",
            "model_catalog": model_catalog,
            "model_voice_modes": model_voice_modes,
            "settings": {"nested": {"keep": [1]}},
            "request_defaults": {"sampling": {"seed": [7]}},
            "extra_body": {"nested": ["extra"]},
            "speech_path": "/v1/audio/speech",
            "voice_catalogues": {"fixture_breeze": ["Fixture"]},
            "voice_metadata": {"Fixture": {"profile": {"tags": ["test"]}}},
        }],
    }


def cached_native_service(settings, cache):
    return cached_service(settings, cache, "audio_cpp")


def cached_service(settings, cache, service_id):
    rows = catalogue._shared_service_configs(settings, cache)
    return cast(dict[str, Any], next(row for row in rows if row.get("id") == service_id))


def seed_cached_service(settings, cache, service_id, *, adapter, provider):
    rows = catalogue._shared_service_configs(settings, cache)
    provider_config = next(
        row for row in settings["provider_configs"]
        if isinstance(row, dict) and row.get("id") == service_id
    )
    endpoint = deepcopy(provider_config)
    endpoint.update({"id": service_id, "adapter": adapter, "provider": provider})
    rows[:] = [row for row in rows if row.get("id") != service_id]
    rows.append(endpoint)
    return endpoint


_FULL_SERVICE_LOOKUP = catalogue.get_service_config


def full_lookup(settings, service_name_or_id, _cache=None, **_ignored):
    """Control for the previous full-copy call shape."""
    return _FULL_SERVICE_LOOKUP(settings, service_name_or_id, _cache)


class DeepcopyBomb:
    def __deepcopy__(self, memo):
        raise AssertionError("an unrequested provider field was copied")


class CopyCountDict(dict[str, Any]):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.copy_count = 0

    def __deepcopy__(self, memo):
        self.copy_count += 1
        copied = type(self)()
        memo[id(self)] = copied
        copied.update({deepcopy(key, memo): deepcopy(value, memo) for key, value in self.items()})
        return copied


def test_default_lookup_keeps_full_metadata_and_cache_mutation_isolation():
    settings = native_settings()
    source_before = deepcopy(settings)
    cache = {}

    first = cast(dict[str, Any] | None, catalogue.get_service_config(settings, "audio_cpp", cache))
    assert first is not None
    assert {
        "default_model",
        "model_catalog",
        "model_voice_modes",
        "voice_catalogues",
        "voice_metadata",
    } <= first.keys()
    expected = deepcopy(first)
    first["model_catalog"][0]["nested"]["choices"].append("response")
    first["request_defaults"]["sampling"]["seed"].append(99)

    assert catalogue.get_service_config(settings, "audio_cpp", cache) == expected
    assert settings == source_before


def test_sparse_fields_copy_only_requested_nested_values_and_keep_aliases():
    settings = native_settings()
    source_before = deepcopy(settings)
    cache = {}
    cached_service = cached_native_service(settings, cache)
    projected = cast(dict[str, Any] | None, catalogue.get_service_config(
        settings,
        "audio_cpp",
        cache,
        _fields=frozenset({"id", "api_base", "model_catalog"}),
    ))
    assert projected is not None
    assert set(projected) == {"id", "api_base", "model_catalog"}

    # The requested projection owns its normal nested data and leaves the
    # cached service and caller settings alone.
    projected["model_catalog"][0]["nested"]["choices"].append("response")
    assert cached_service["model_catalog"][0]["nested"]["choices"] == ["configured"]
    assert settings == source_before

    poison = DeepcopyBomb()
    cached_service["model_catalog"].append({"id": "unused", "payload": poison})
    unrequested = cast(dict[str, Any] | None, catalogue.get_service_config(
        settings,
        "audio_cpp",
        cache,
        _fields=frozenset({"id", "api_base"}),
    ))
    assert unrequested is not None
    assert unrequested == {"id": "audio_cpp", "api_base": "http://native.invalid:8060"}
    assert "model_catalog" not in unrequested
    assert cached_service["model_catalog"][-1]["payload"] is poison
    cached_service["model_catalog"].pop()

    assert catalogue.get_service_config(
        settings, "audio_cpp", cache, _fields=frozenset()
    ) == {}
    assert catalogue.get_service_config(
        settings, "audio_cpp", cache, _fields=frozenset({"not_a_field"})
    ) == {}
    assert catalogue.get_service_config(
        settings, "missing-service", cache, _fields=frozenset({"id"})
    ) is None
    for alias in ("audio.cpp", "audio-cpp", "audiocpp"):
        aliased = cast(dict[str, Any] | None, catalogue.get_service_config(settings, alias, cache))
        assert aliased is not None
        assert aliased["id"] == "audio_cpp"


def test_full_list_field_projection_keeps_order_and_owns_only_requested_fields():
    settings = native_settings()
    source_before = deepcopy(settings)
    cache = {}
    full = catalogue.get_service_configs(settings, cache)
    cached_native = cached_native_service(settings, cache)

    projected = catalogue.get_service_configs(
        settings,
        cache,
        _fields=frozenset({"id", "model_catalog"}),
    )
    assert [row["id"] for row in projected] == [row["id"] for row in full]
    native = cast(dict[str, Any], next(row for row in projected if row["id"] == "audio_cpp"))
    native_catalog = cast(list[dict[str, Any]], native["model_catalog"])
    native_catalog[0]["nested"]["choices"].append("caller mutation")
    assert cached_native["model_catalog"][0]["nested"]["choices"] == ["configured"]

    empty = catalogue.get_service_configs(settings, cache, _fields=frozenset())
    unknown = catalogue.get_service_configs(
        settings,
        cache,
        _fields=frozenset({"not_a_catalogue_field"}),
    )
    assert empty == [{} for _ in full]
    assert unknown == [{} for _ in full]
    assert settings == source_before

    poison = DeepcopyBomb()
    cached_native["model_catalog"].append({"id": "unselected", "body": poison})
    sparse = catalogue.get_service_configs(
        settings,
        cache,
        _fields=frozenset({"id", "api_base"}),
    )
    assert all(set(row) <= {"id", "api_base"} for row in sparse)
    assert cached_native["model_catalog"][-1]["body"] is poison
    cached_native["model_catalog"].pop()


def test_model_catalog_projection_filters_casefolded_matches_in_original_order():
    records = [
        {"id": " Target ", "family": "breeze_tts", "label": "first"},
        {"id": "target", "family": "qwen3_tts", "label": "second"},
        {"id": "elsewhere", "label": "unselected"},
        "not a mapping",
        {"label": "missing id"},
    ]
    settings = native_settings(
        model_catalog=records,
        model_voice_modes={"TARGET": "design", "target ": "prebuilt"},
    )
    cache = {}
    full = cast(dict[str, Any] | None, catalogue.get_service_config(settings, "audio_cpp", cache))
    projected = cast(dict[str, Any] | None, catalogue.get_service_config(
        settings,
        "audio_cpp",
        cache,
        _fields=frozenset({"model_catalog", "model_voice_modes"}),
        _model_catalog_id=" TARGET ",
    ))

    assert full is not None and projected is not None
    assert [item["label"] for item in projected["model_catalog"]] == ["first", "second"]
    assert tts_handler._audio_cpp_model_metadata("target", projected) == (
        tts_handler._audio_cpp_model_metadata("target", full)
    )
    projected["model_catalog"][0]["label"] = "changed"
    fresh_projection = cast(dict[str, Any] | None, catalogue.get_service_config(
        settings,
        "audio_cpp",
        cache,
        _fields=frozenset({"model_catalog"}),
        _model_catalog_id="target",
    ))
    assert fresh_projection is not None
    assert fresh_projection["model_catalog"][0]["label"] == "first"

    no_match = cast(dict[str, Any] | None, catalogue.get_service_config(
        settings,
        "audio_cpp",
        cache,
        _fields=frozenset({"model_catalog"}),
        _model_catalog_id="absent",
    ))
    assert no_match is not None
    assert no_match == {"model_catalog": []}

    missing_settings = native_settings()
    missing_cache = {}
    cached_native_service(missing_settings, missing_cache).pop("model_catalog")
    missing = cast(dict[str, Any] | None, catalogue.get_service_config(
        missing_settings,
        "audio_cpp",
        missing_cache,
        _fields=frozenset({"model_catalog", "model_voice_modes"}),
        _model_catalog_id="fixture_breeze",
    ))
    assert missing is not None
    assert "model_catalog" not in missing


def test_adapt_runtime_settings_matches_full_lookup_with_sparse_service_fields(monkeypatch):
    settings = {"service": "audio.cpp", "voice": "Fixture", **native_settings()}
    source_before = deepcopy(settings)
    original_lookup = tts_handler.get_service_config
    actual_calls = []

    def record_sparse(*args, **kwargs):
        actual_calls.append(kwargs.copy())
        return original_lookup(*args, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(tts_handler, "get_service_config", record_sparse)
        actual = adapt_runtime_settings("tts", settings, {})

    with monkeypatch.context() as scoped:
        scoped.setattr(tts_handler, "get_service_config", full_lookup)
        expected = adapt_runtime_settings("tts", settings, {})

    assert actual == expected
    assert actual_calls[0]["_fields"] == frozenset({"id", "api_base"})
    assert settings == source_before


def test_material_settings_matches_full_lookup_and_fetches_selected_audio_cpp_metadata(
    monkeypatch,
):
    settings = {
        "service": "audio_cpp",
        "voice": "Fixture",
        # Exercise the established default_model fallback.
        "audio_cpp_model_settings": {"fixture_breeze": {"temperature": 0.7}},
        **native_settings(
            model_catalog=[{
                "id": "fixture_breeze",
                "family": "qwen3_tts",
                "voice_mode": "cloning",
                "nested": {"values": ["selected"]},
            }, {
                "id": "other_model",
                "family": "breeze_tts",
                "nested": {"values": ["not selected"]},
            }],
            model_voice_modes={"fixture_breeze": "design", "other_model": "prebuilt"},
        ),
    }
    snapshot = {"tts": settings, "audio": {}}
    source_before = deepcopy(snapshot)
    original_lookup = tts_handler.get_service_config
    original_metadata = tts_handler._audio_cpp_model_metadata
    actual_calls = []
    metadata_inputs = []
    metadata_results = []

    def record_sparse(*args, **kwargs):
        actual_calls.append(kwargs.copy())
        return original_lookup(*args, **kwargs)

    def record_metadata(model, endpoint):
        metadata_inputs.append(deepcopy(endpoint))
        result = original_metadata(model, endpoint)
        metadata_results.append(result)
        return result

    with monkeypatch.context() as scoped:
        scoped.setattr(tts_handler, "get_service_config", record_sparse)
        scoped.setattr(tts_handler, "_audio_cpp_model_metadata", record_metadata)
        actual = _material_settings(snapshot, {})

    with monkeypatch.context() as scoped:
        scoped.setattr(tts_handler, "get_service_config", full_lookup)
        expected = _material_settings(snapshot, {})

    assert actual == expected
    assert len(actual_calls) == 3
    assert actual_calls[0]["_fields"] == frozenset({"id", "api_base"})
    assert actual_calls[1]["_fields"] == frozenset({
        "id", "adapter", "api_base", "settings", "request_defaults",
        "extra_body", "speech_path", "default_model",
    })
    assert actual_calls[2]["_fields"] == frozenset({"model_catalog", "model_voice_modes"})
    assert actual_calls[2]["_model_catalog_id"] == "fixture_breeze"
    assert len(metadata_inputs) == 1
    assert [row["id"] for row in metadata_inputs[0]["model_catalog"]] == ["fixture_breeze"]
    assert metadata_results[0]["family"] == "qwen3_tts"
    assert metadata_results[0]["voice_mode"] == "design"
    assert actual["audio_cpp_model_settings"] == {
        "fixture_breeze": {"temperature": 0.7},
    }
    assert "model_catalog" not in actual["provider"]
    assert "model_voice_modes" not in actual["provider"]
    assert settings == source_before["tts"]
    assert snapshot == source_before


@pytest.mark.parametrize(
    ("has_selected_model_entry", "selected_options"),
    [(False, None), (True, None), (True, {})],
)
def test_material_settings_copies_no_unselected_model_bodies(
    has_selected_model_entry, selected_options, monkeypatch,
):
    settings = {"service": "audio_cpp", **native_settings()}
    if has_selected_model_entry:
        settings["audio_cpp_model_settings"] = {"fixture_breeze": selected_options}
    source_before = deepcopy(settings)
    cache = {}
    service = cached_native_service(settings, cache)
    selected = CopyCountDict({
        "id": "fixture_breeze",
        "family": "qwen3_tts",
        "nested": {"values": ["selected"]},
    })
    unselected = CopyCountDict({
        "id": "other_model",
        "family": "breeze_tts",
        "nested": {"values": ["unselected"]},
    })
    service["model_catalog"] = [selected, unselected]
    cached_catalogue = service["model_catalog"]
    original_lookup = tts_handler.get_service_config
    lookup_calls = []

    def record_lookup(*args, **kwargs):
        lookup_calls.append(kwargs.copy())
        return original_lookup(*args, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(tts_handler, "get_service_config", record_lookup)
        result = _material_settings({"tts": settings, "audio": {}}, cache)

    assert selected.copy_count == (1 if has_selected_model_entry else 0)
    assert unselected.copy_count == 0
    assert service["model_catalog"] is cached_catalogue
    assert cached_catalogue[0] is selected and cached_catalogue[1] is unselected
    assert selected["nested"]["values"] == ["selected"]
    assert unselected["nested"]["values"] == ["unselected"]
    assert settings == source_before
    assert "model_catalog" not in result["provider"]
    assert "model_voice_modes" not in result["provider"]
    assert len(lookup_calls) == 2 + int(has_selected_model_entry)
    if has_selected_model_entry:
        assert lookup_calls[-1]["_fields"] == frozenset({"model_catalog", "model_voice_modes"})
        assert lookup_calls[-1]["_model_catalog_id"] == "fixture_breeze"


@pytest.mark.parametrize("selected_options", [None, {}])
def test_empty_and_null_audio_cpp_model_options_match_full_lookup(
    selected_options, monkeypatch,
):
    settings = {
        "service": "audio_cpp",
        "audio_cpp_model_settings": {"fixture_breeze": selected_options},
        **native_settings(),
    }
    snapshot = {"tts": settings, "audio": {}}
    actual = _material_settings(snapshot, {})

    with monkeypatch.context() as scoped:
        scoped.setattr(tts_handler, "get_service_config", full_lookup)
        expected = _material_settings(snapshot, {})

    assert actual == expected


def test_cached_language_capability_and_compiled_outputs_match_full_lookup(monkeypatch):
    settings = {"service": "audio_cpp", "language": "auto", **native_settings()}
    source_before = deepcopy(settings)
    language_calls = []
    original_capability_lookup = _FULL_SERVICE_LOOKUP

    def record_language_lookup(*args, **kwargs):
        language_calls.append(kwargs.copy())
        return original_capability_lookup(*args, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(catalogue, "get_service_config", record_language_lookup)
        actual_language = resolve_tts_language_support(settings, _service_config_cache={})

    assert len(language_calls) == 2
    assert language_calls[0]["_fields"] == frozenset({
        "id", "name", "adapter", "provider", "default_model",
    })
    assert language_calls[1]["_fields"] == frozenset({"model_catalog"})
    assert language_calls[1]["_model_catalog_id"] == "fixture_breeze"

    with monkeypatch.context() as scoped:
        scoped.setattr(catalogue, "get_service_config", full_lookup)
        expected_language = resolve_tts_language_support(settings, _service_config_cache={})
    assert actual_language == expected_language

    capability_calls = []

    def record_capability_lookup(*args, **kwargs):
        capability_calls.append(kwargs.copy())
        return original_capability_lookup(*args, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(catalogue, "get_service_config", record_capability_lookup)
        actual_capabilities = resolve_capabilities(settings, _service_config_cache={})

    assert len(capability_calls) == 2
    assert capability_calls[0]["_fields"] == frozenset({
        "id", "name", "adapter", "provider", "profile_id", "base_url", "api_base",
        "backend_version", "default_model", "default_voice", "default_voices",
        "model_voice_modes",
    })
    assert capability_calls[1]["_fields"] == frozenset({"model_catalog"})
    assert capability_calls[1]["_model_catalog_id"] == "fixture_breeze"

    with monkeypatch.context() as scoped:
        scoped.setattr(catalogue, "get_service_config", full_lookup)
        expected_capabilities = resolve_capabilities(settings, _service_config_cache={})
    assert actual_capabilities == expected_capabilities

    with monkeypatch.context() as scoped:
        actual_compiled = compile_performance(
            "The scene is quiet.", settings, _service_config_cache={}
        )
    with monkeypatch.context() as scoped:
        scoped.setattr(catalogue, "get_service_config", full_lookup)
        expected_compiled = compile_performance(
            "The scene is quiet.", settings, _service_config_cache={}
        )
    assert actual_compiled == expected_compiled
    assert actual_compiled.fingerprint == expected_compiled.fingerprint
    assert settings == source_before


@pytest.mark.parametrize(
    ("service", "choices", "selected_field"),
    [
        ("silero", {"silero_model": "silero-top", "xtts_model": "xtts-middle", "model": "model-low"}, "silero_model"),
        ("silero", {"xtts_model": "xtts-middle", "model": "model-low"}, "xtts_model"),
        ("silero", {"model": "model-low"}, "model"),
        ("silero", {}, "default_model"),
        ("elevenlabs", {"elevenlabs_model": "elevenlabs-top", "xtts_model": "xtts-middle", "model": "model-low"}, "elevenlabs_model"),
        ("elevenlabs", {"xtts_model": "xtts-middle", "model": "model-low"}, "xtts_model"),
        ("elevenlabs", {"model": "model-low"}, "model"),
        ("elevenlabs", {}, "default_model"),
    ],
)
def test_cached_preflight_preserves_silero_and_elevenlabs_model_precedence(
    service, choices, selected_field, monkeypatch,
):
    settings = {"service": service, "language": "auto", **choices}
    source_before = deepcopy(settings)
    service_record = _FULL_SERVICE_LOOKUP(settings, service, {})
    expected_model = (
        str(choices[selected_field])
        if selected_field != "default_model"
        else str((service_record or {}).get("default_model") or "")
    )
    if not expected_model:
        expected_model = (
            catalogue.SILERO_DEFAULT_MODEL
            if service == "silero"
            else catalogue.ELEVENLABS_TTS_DEFAULT_MODEL
        )

    language_calls = []

    def record_language_lookup(*args, **kwargs):
        language_calls.append(kwargs.copy())
        return _FULL_SERVICE_LOOKUP(*args, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(catalogue, "get_service_config", record_language_lookup)
        actual_language = resolve_tts_language_support(settings, _service_config_cache={})

    assert len(language_calls) == 2
    assert language_calls[1]["_model_catalog_id"] == expected_model
    with monkeypatch.context() as scoped:
        scoped.setattr(catalogue, "get_service_config", full_lookup)
        expected_language = resolve_tts_language_support(settings, _service_config_cache={})
    assert actual_language == expected_language

    capability_models = []
    original_capability_builder = speech_performance.capabilities_for_endpoint_model

    def record_capability_model(model, endpoint, **kwargs):
        capability_models.append(model)
        return original_capability_builder(model, endpoint, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(speech_performance, "capabilities_for_endpoint_model", record_capability_model)
        actual_capabilities = resolve_capabilities(settings, _service_config_cache={})

    assert capability_models == [expected_model]
    with monkeypatch.context() as scoped:
        scoped.setattr(catalogue, "get_service_config", full_lookup)
        expected_capabilities = resolve_capabilities(settings, _service_config_cache={})
    assert actual_capabilities == expected_capabilities
    assert settings == source_before


@pytest.mark.parametrize("resolver", ["language", "capabilities"])
def test_cached_preflight_copies_only_the_selected_model_body(resolver):
    settings = {
        "service": "audio_cpp",
        "model": "fixture_breeze",
        "language": "auto",
        **native_settings(),
    }
    source_before = deepcopy(settings)
    cache = {}
    service = cached_native_service(settings, cache)
    selected = CopyCountDict({
        "id": "fixture_breeze",
        "family": "qwen3_tts",
        "voice_mode": "design",
        "language_support": {"languages": ["en"]},
        "nested": {"values": ["selected"]},
    })
    unselected = CopyCountDict({
        "id": "other_model",
        "payload": DeepcopyBomb(),
        "nested": {"values": ["unselected"]},
    })
    service["model_catalog"] = [selected, unselected]
    cached_catalogue = service["model_catalog"]

    if resolver == "language":
        resolve_tts_language_support(settings, _service_config_cache=cache)
    else:
        resolve_capabilities(settings, _service_config_cache=cache)

    assert selected.copy_count == 1
    assert unselected.copy_count == 0
    assert service["model_catalog"] is cached_catalogue
    assert cached_catalogue[0] is selected and cached_catalogue[1] is unselected
    assert selected["nested"]["values"] == ["selected"]
    assert unselected["nested"]["values"] == ["unselected"]
    assert settings == source_before


def test_cached_audio_cpp_capabilities_preserve_exact_row_precedence(monkeypatch):
    settings = {
        "service": "audio_cpp",
        "model": "fixture_breeze",
        **native_settings(
            model_catalog=[
                {
                    "id": "Fixture_Breeze",
                    "family": "fish_audio_s2",
                    "voice_mode": "design",
                    "label": "case variant",
                },
                {
                    "id": "fixture_breeze",
                    "family": "qwen3_tts",
                    "voice_mode": "cloning",
                    "label": "exact row",
                },
            ],
            model_voice_modes={"fixture_breeze": "prebuilt"},
        ),
    }
    source_before = deepcopy(settings)
    endpoint_captures = []
    original_builder = speech_performance.capabilities_for_endpoint_model

    def capture_endpoint(model, endpoint, **kwargs):
        endpoint_captures.append({
            "model": model,
            "family": kwargs.get("family"),
            "voice_mode": kwargs.get("voice_mode"),
            "model_catalog": deepcopy(endpoint.get("model_catalog")),
        })
        return original_builder(model, endpoint, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(speech_performance, "capabilities_for_endpoint_model", capture_endpoint)
        actual = resolve_capabilities(settings, _service_config_cache={})

    with monkeypatch.context() as scoped:
        scoped.setattr(catalogue, "get_service_config", full_lookup)
        expected = resolve_capabilities(settings, _service_config_cache={})

    assert actual == expected
    assert len(endpoint_captures) == 1
    assert endpoint_captures[0]["model"] == "fixture_breeze"
    assert endpoint_captures[0]["family"] == "qwen3_tts"
    assert endpoint_captures[0]["voice_mode"] == "cloning"
    assert [
        item["id"] for item in endpoint_captures[0]["model_catalog"]
    ] == ["Fixture_Breeze", "fixture_breeze"]
    assert settings == source_before


def test_language_lookup_skips_metadata_when_selected_model_is_empty(monkeypatch):
    calls = []

    def lookup_without_model(settings, selector, _cache=None, **kwargs):
        calls.append(kwargs.copy())
        assert "_model_catalog_id" not in kwargs
        return {
            "id": selector,
            "adapter": "generic_json",
            "provider": "custom",
            "default_model": "",
        }

    with monkeypatch.context() as scoped:
        scoped.setattr(catalogue, "get_service_config", lookup_without_model)
        support = resolve_tts_language_support(
            {"service": "custom_fixture"}, _service_config_cache={}
        )

    assert support is None
    assert len(calls) == 1
    assert calls[0]["_fields"] == frozenset({
        "id", "name", "adapter", "provider", "default_model",
    })


def test_uncached_lookups_remain_full_and_explicit_endpoints_skip_catalogue(monkeypatch):
    settings = {
        "service": "audio_cpp",
        "model": "fixture_breeze",
        "language": "auto",
        **native_settings(),
    }
    uncached_calls = []

    def record_uncached(*args, **kwargs):
        uncached_calls.append(kwargs.copy())
        return _FULL_SERVICE_LOOKUP(*args, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(catalogue, "get_service_config", record_uncached)
        resolve_tts_language_support(settings, _service_config_cache=None)
    assert len(uncached_calls) == 1
    assert "_fields" not in uncached_calls[0]
    assert "_model_catalog_id" not in uncached_calls[0]

    uncached_calls.clear()
    with monkeypatch.context() as scoped:
        scoped.setattr(catalogue, "get_service_config", record_uncached)
        resolve_capabilities(settings, _service_config_cache=None)
    assert len(uncached_calls) == 1
    assert "_fields" not in uncached_calls[0]
    assert "_model_catalog_id" not in uncached_calls[0]

    endpoint = {
        "id": "audio_cpp",
        "adapter": "audio_cpp",
        "provider": "audio_cpp",
        "model_catalog": [{
            "id": "fixture_breeze", "family": "qwen3_tts", "voice_mode": "design",
        }],
        "model_voice_modes": {"fixture_breeze": "design"},
    }

    def unexpected_catalogue_lookup(*_args, **_kwargs):
        pytest.fail("an explicit endpoint must not trigger a catalogue lookup")

    with monkeypatch.context() as scoped:
        scoped.setattr(catalogue, "get_service_config", unexpected_catalogue_lookup)
        resolve_tts_language_support(settings, endpoint=endpoint, _service_config_cache={})
        resolve_capabilities(settings, endpoint=endpoint, _service_config_cache={})
        compile_performance(
            "The scene is quiet.", settings, endpoint=endpoint, _service_config_cache={}
        )


def _custom_service_settings(service_id, *, default_model, model_catalog, **config):
    provider_config = {
        "id": service_id,
        "name": service_id,
        "api_base": "http://fixture.invalid",
        "models": [default_model] if default_model else [],
        "default_model": default_model,
        "model_catalog": model_catalog,
        **config,
    }
    return {"service": service_id, "provider_configs": [provider_config]}


def test_cached_azure_speech_uses_canonical_model_and_endpoint_default_voice(monkeypatch):
    from pandrator.logic.azure_speech_emotions import (
        canonical_azure_speech_model,
        canonical_azure_speech_voice,
    )

    service_id = "azure-speech-fixture"
    raw_model = "MAI-Voice-2"
    canonical_model = canonical_azure_speech_model(raw_model) or raw_model
    raw_voice = "en-US-Ethan:MAI-Voice-2"
    expected_voice = canonical_azure_speech_voice(canonical_model, raw_voice) or raw_voice
    assert canonical_model == "MAI-Voice-2.1"
    assert canonical_model != raw_model
    assert expected_voice == "en-US-Ethan:MAI-Voice-2.1"
    assert expected_voice != raw_voice
    settings = _custom_service_settings(
        service_id,
        default_model=raw_model,
        model_catalog=[{"id": canonical_model, "upstream_model_id": "unused"}],
        default_voice="en-US-AriaNeural",
        default_voices={canonical_model: raw_voice},
    )
    source_before = deepcopy(settings)
    original_builder = speech_performance.capabilities_for_endpoint_model
    selections = []

    def record_selection(model, endpoint, **kwargs):
        selections.append((model, kwargs.get("voice"), endpoint.get("model_catalog")))
        return original_builder(model, endpoint, **kwargs)

    def prepared_cache():
        cache = {}
        seed_cached_service(
            settings,
            cache,
            service_id,
            adapter="azure_speech",
            provider="azure",
        )
        return cache

    with monkeypatch.context() as scoped:
        scoped.setattr(speech_performance, "capabilities_for_endpoint_model", record_selection)
        actual = resolve_capabilities(settings, _service_config_cache=prepared_cache())

    with monkeypatch.context() as scoped:
        scoped.setattr(catalogue, "get_service_config", full_lookup)
        expected = resolve_capabilities(settings, _service_config_cache=prepared_cache())

    assert actual == expected
    assert selections == [(canonical_model, expected_voice, [{
        "id": canonical_model, "upstream_model_id": "unused",
    }])]

    compile_lookup_calls = []

    def record_compile_lookup(*args, **kwargs):
        compile_lookup_calls.append(kwargs.copy())
        return _FULL_SERVICE_LOOKUP(*args, **kwargs)

    compile_cache = prepared_cache()
    compile_service = cached_service(settings, compile_cache, service_id)
    poison = DeepcopyBomb()
    compile_service["model_catalog"].append({"id": "unselected-model", "payload": poison})
    with monkeypatch.context() as scoped:
        scoped.setattr(catalogue, "get_service_config", record_compile_lookup)
        actual_compiled = compile_performance(
            "The scene is quiet.", settings, _service_config_cache=compile_cache
        )

    with monkeypatch.context() as scoped:
        scoped.setattr(catalogue, "get_service_config", full_lookup)
        expected_compiled = compile_performance(
            "The scene is quiet.", settings, _service_config_cache=prepared_cache()
        )

    voice_lookups = [
        call for call in compile_lookup_calls
        if call.get("_fields") == frozenset({"default_voice", "default_voices"})
    ]
    assert len(voice_lookups) == 1
    assert "_model_catalog_id" not in voice_lookups[0]
    assert actual_compiled == expected_compiled
    assert actual_compiled.fingerprint == expected_compiled.fingerprint
    assert compile_service["model_catalog"][-1]["payload"] is poison
    assert settings == source_before


def test_cached_azure_capability_projection_preserves_upstream_model_mapping(monkeypatch):
    service_id = "azure-openai-fixture"
    deployment = "fixture-deployment"
    upstream = "gpt-4o-mini-tts"
    settings = _custom_service_settings(
        service_id,
        default_model=deployment,
        model_catalog=[
            {"id": deployment, "upstream_model_id": upstream},
            {"id": "other-deployment", "upstream_model_id": "tts-1-hd"},
        ],
    )
    source_before = deepcopy(settings)
    calls = []

    def record_lookup(*args, **kwargs):
        calls.append(kwargs.copy())
        return _FULL_SERVICE_LOOKUP(*args, **kwargs)

    def prepared_cache():
        cache = {}
        seed_cached_service(
            settings,
            cache,
            service_id,
            adapter="azure_openai",
            provider="azure",
        )
        return cache

    with monkeypatch.context() as scoped:
        scoped.setattr(catalogue, "get_service_config", record_lookup)
        actual = resolve_capabilities(settings, _service_config_cache=prepared_cache())

    assert len(calls) == 2
    assert calls[0]["_fields"] == frozenset({
        "id", "name", "adapter", "provider", "profile_id", "base_url", "api_base",
        "backend_version", "default_model", "default_voice", "default_voices",
        "model_voice_modes",
    })
    assert calls[1]["_fields"] == frozenset({"model_catalog"})
    assert calls[1]["_model_catalog_id"] == deployment

    with monkeypatch.context() as scoped:
        scoped.setattr(catalogue, "get_service_config", full_lookup)
        expected = resolve_capabilities(settings, _service_config_cache=prepared_cache())

    expected_upstream = speech_performance.capabilities_for_model(upstream, backend="openai")
    assert actual == expected
    assert actual["instructions"] == expected_upstream["instructions"]
    assert settings == source_before


def test_cold_selected_service_lookup_matches_full_selection_for_native_custom_alias_and_miss():
    native = native_settings()
    native_profiles = catalogue.get_service_configs(native)
    native_expected = next(row for row in native_profiles if row.get("id") == "audio_cpp")
    assert catalogue.get_service_config(native, "audio_cpp") == native_expected

    alias_expected = next(row for row in native_profiles if row.get("id") == "audio_cpp")
    assert catalogue.get_service_config(native, "audio.cpp") == alias_expected

    custom = _custom_service_settings(
        "fixture-custom-provider",
        default_model="fixture-model",
        model_catalog=[{"id": "fixture-model", "family": "qwen3_tts"}],
        adapter="openai_compatible",
    )
    custom_profiles = catalogue.get_service_configs(custom)
    custom_expected = next(row for row in custom_profiles if row.get("is_custom"))
    assert catalogue.get_service_config(custom, "Fixture-Custom-Provider") == custom_expected
    assert catalogue.get_service_config(native, "missing-service") is None


@pytest.mark.parametrize(
    ("explicit_api_base", "expected_host"),
    [
        ("http://explicit.invalid", "explicit.invalid"),
        ("", "legacy.invalid"),
    ],
)
def test_cold_selected_legacy_and_explicit_config_precedence_matches_full_list(
    explicit_api_base: str, expected_host: str,
):
    service_name = "Fixture Legacy"
    service_id = catalogue._normalize_service_id(service_name)
    settings = {
        "provider_configs": [{
            "id": service_id,
            "name": service_name,
            "api_base": explicit_api_base,
            "models": ["explicit-model"],
            "default_model": "explicit-model",
            "adapter": "openai_compatible",
        }],
        "openai_audio_endpoints_json": json.dumps([{
            "name": service_name,
            "base_url": "http://legacy.invalid",
            "models": ["legacy-model"],
            "default_model": "legacy-model",
        }]),
    }
    full_profiles = catalogue.get_service_configs(settings)
    expected = next(row for row in full_profiles if row.get("id") == service_id)

    selected = catalogue.get_service_config(settings, service_name)

    assert selected == expected
    assert selected is not None
    assert expected_host in str(selected["api_base"])


def test_selected_service_construction_never_reads_or_writes_full_cache_key():
    settings = native_settings()
    cache_key = ("service_configs", catalogue._service_config_cache_key(settings))
    sentinel = [{"id": "full-catalogue-sentinel"}]
    cache = {cache_key: sentinel}

    selected = catalogue._shared_service_configs(
        settings, cache, _selected_service_id="xtts"
    )

    assert [row["id"] for row in selected] == ["xtts"]
    assert cache == {cache_key: sentinel}
    assert cache[cache_key] is sentinel


def test_cold_other_provider_lookup_does_not_copy_unselected_native_override_body():
    poison = DeepcopyBomb()
    settings = {
        "provider_configs": [{
            "id": "audio_cpp",
            "api_base": "http://native.invalid:8060",
            "model_catalog": [{"id": "unused", "payload": poison}],
        }],
    }

    selected = catalogue.get_service_config(settings, "openai")

    assert selected is not None
    assert selected["id"] == "openai"
    assert settings["provider_configs"][0]["model_catalog"][0]["payload"] is poison


def test_native_model_catalogue_override_skips_discarded_static_body_and_is_owned(
    monkeypatch,
):
    poison = DeepcopyBomb()
    monkeypatch.setattr(
        catalogue,
        "AUDIO_CPP_MODEL_CATALOG",
        [{"id": "static-model", "payload": poison}],
    )
    settings = {
        "service": "audio_cpp",
        **native_settings(
            model_catalog=[{
                "id": "fixture_breeze",
                "family": "qwen3_tts",
                "nested": {"values": ["override"]},
            }],
        ),
    }
    source_before = deepcopy(settings)

    selected = catalogue.get_service_config(settings, "audio_cpp")

    assert selected is not None
    selected_catalogue = cast(list[dict[str, Any]], selected["model_catalog"])
    assert [{key: row[key] for key in ("id", "family", "nested")}
        for row in selected_catalogue] == [{
            "id": "fixture_breeze",
            "family": "qwen3_tts",
            "nested": {"values": ["override"]},
        }]
    assert "expressive_capabilities" in selected_catalogue[0]
    assert selected["model_catalog"] is not settings["provider_configs"][0]["model_catalog"]
    selected_catalogue[0]["nested"]["values"].append("caller mutation")
    assert settings == source_before
    assert catalogue.AUDIO_CPP_MODEL_CATALOG[0]["payload"] is poison
