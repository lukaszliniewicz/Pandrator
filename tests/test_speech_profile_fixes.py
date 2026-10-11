"""Focused tests for conservative speech capability profile metadata."""

import pytest

from pandrator.logic.speech_capabilities import (
    CAPABILITY_VERSION,
    capabilities_for_endpoint_model,
    capabilities_for_model,
    capability_backend,
)


def test_unknown_profile_has_explicit_conservative_transport_defaults():
    profile = capabilities_for_model("unlisted-model", backend="openai_compatible")

    assert profile["profile_version"] == CAPABILITY_VERSION
    assert profile["native_instruction_scope"] == []
    assert profile["phrase_instruction_transport"] == "none"
    assert profile["phrase_instruction_reliability"] == "none"
    assert profile["semantic_context_transport"] == "none"
    assert profile["recommended_instruction_scope"] == "none"


@pytest.mark.parametrize(
    ("model", "backend", "family"),
    [
        ("gpt-4o-mini-tts", "openai", ""),
        ("model", "audio_cpp", "breeze_tts"),
    ],
)
def test_field_delivery_and_voice_delivery_profiles_use_request_and_phrase_hints(
    model: str, backend: str, family: str
):
    profile = capabilities_for_model(model, backend=backend, family=family)

    assert profile["instruction_kind"] in {"delivery", "voice_and_delivery"}
    assert profile["native_instruction_scope"] == ["request"]
    assert profile["phrase_instruction_transport"] == "quoted_hint"
    assert profile["phrase_instruction_reliability"] == "best_effort"
    assert profile["recommended_instruction_scope"] == "none"


def test_voice_description_keeps_phrase_instruction_transport_disabled():
    profile = capabilities_for_model(
        "moss_voicegen_bf16_codec_f16_decode", backend="audio_cpp"
    )

    assert profile["instruction_kind"] == "voice_description"
    assert profile["phrase_instruction_transport"] == "none"
    assert profile["phrase_instruction_reliability"] == "none"


def test_fish_and_eleven_inline_profiles_mark_phrase_hints_as_best_effort():
    fish = capabilities_for_model("fish_speech_s2_pro", backend="audio_cpp", family="fish_audio_s2")
    eleven = capabilities_for_model("eleven_v3", backend="elevenlabs")

    assert fish["phrase_instruction_transport"] == "inline_hint"
    assert fish["phrase_instruction_reliability"] == "best_effort"
    assert fish["recommended_instruction_scope"] == "request"
    assert eleven["phrase_instruction_transport"] == "inline_hint"
    assert eleven["phrase_instruction_reliability"] == "best_effort"


def test_gemini_context_transports_are_route_and_generation_specific():
    gemini38 = capabilities_for_model("gemini-3.8-flash-tts", backend="gemini")
    legacy = capabilities_for_model(
        "gemini-2.5-flash-preview-tts", backend="vertex_ai"
    )

    assert gemini38["native_instruction_scope"] == ["request"]
    assert gemini38["phrase_instruction_transport"] == "quoted_hint"
    assert gemini38["phrase_instruction_reliability"] == "best_effort"
    assert gemini38["semantic_context_transport"] == "instruction_hint"
    assert legacy["native_instruction_scope"] == ["request"]
    assert legacy["phrase_instruction_transport"] == "inline_hint"
    assert legacy["phrase_instruction_reliability"] == "best_effort"
    assert legacy["semantic_context_transport"] == "guided_prompt"
    assert legacy["event_tags"] == {"sigh": "sigh"}


def test_native_field_context_and_voxcpm_request_prefix_have_distinct_transport():
    eleven = capabilities_for_model("eleven_v4", backend="elevenlabs")
    voxcpm = capabilities_for_model("VoxCPM2", backend="audio_cpp", family="voxcpm2")

    assert eleven["semantic_context_transport"] == "native_fields"
    assert voxcpm["phrase_instruction_transport"] == "none"
    assert voxcpm["phrase_instruction_reliability"] == "none"


def test_eleven_exhale_and_exact_v4_pause_profiles():
    for model in ("eleven_v3", "eleven_v4"):
        profile = capabilities_for_model(model, backend="elevenlabs")
        assert profile["event_tags"]["exhale"] == "exhales"

    v4 = capabilities_for_model("eleven_v4", backend="elevenlabs")
    v3 = capabilities_for_model("eleven_v3", backend="elevenlabs")
    v4_turbo = capabilities_for_model("eleven_v4_turbo", backend="elevenlabs")
    assert "exhale" not in v4_turbo["event_tags"]
    assert "pause" not in v3["event_tags"]
    assert v4["event_tags"]["pause"] == "pause"
    assert v4["event_format"] == "inline"
    assert v4["event_pause"] == {
        "format": "inline",
        "short_tag": "pause",
        "long_tag": "long pause",
        "threshold_ms": 1000,
        "max_duration_ms": None,
    }
    assert "pause" not in v4_turbo["event_tags"]
    assert "event_pause" not in v4_turbo


@pytest.mark.parametrize("model", [
    "eleven_multilingual_v2",
    "eleven_flash_v2_5",
    "eleven_flash_v2",
])
def test_only_verified_legacy_eleven_models_map_ssml_pause(model: str):
    profile = capabilities_for_model(model, backend="elevenlabs")

    assert profile["event_tags"] == {"pause": "break"}
    assert profile["event_format"] == "ssml_break"
    assert profile["event_pause"] == {
        "format": "ssml_break",
        "max_duration_ms": 3000,
    }


@pytest.mark.parametrize("model", ["eleven_turbo_v2", "eleven_turbo_v2_5"])
def test_other_legacy_eleven_variants_do_not_inherit_pause(model: str):
    profile = capabilities_for_model(model, backend="elevenlabs")

    assert "pause" not in profile["event_tags"]
    assert "event_pause" not in profile


def test_chatterbox_turbo_event_table_is_route_specific():
    dedicated = capabilities_for_model("chatterbox_turbo", backend="chatterbox")
    audio_cpp = capabilities_for_model(
        "chatterbox_turbo", backend="audio_cpp", family="chatterbox_turbo"
    )

    expected_tags = {
        "laugh": "laugh",
        "chuckle": "chuckle",
        "sigh": "sigh",
        "cough": "cough",
        "gasp": "gasp",
        "clear_throat": "clear throat",
    }
    assert dedicated["event_tags"] == expected_tags
    assert audio_cpp["event_tags"] == expected_tags


@pytest.mark.parametrize(
    "model",
    [
        "auk_base_f16",
        "auk_base_f32",
        "auk_base_q8_0",
        "auk_flash_f16",
        "auk_flash_f32",
        "auk_flash_q8_0",
    ],
)
def test_auk_generator_models_require_voice_description(model: str):
    profile = capabilities_for_model(model, backend="audio_cpp", family="auk")

    assert profile["status"] == "documented"
    assert profile["instructions"] == "field"
    assert profile["instruction_kind"] == "voice_description"
    assert profile["requires_instruction"] is True
    assert profile["instruction_transport"] == "options.instruct"
    assert profile["phrase_instruction_transport"] == "none"
    assert profile["phrase_instruction_reliability"] == "none"


@pytest.mark.parametrize("model", ["auk_qwen_bf16", "auk_qwen_q8_0", "auk_vae_f32"])
def test_auk_components_remain_unknown_models(model: str):
    profile = capabilities_for_model(model, backend="audio_cpp", family="auk")

    assert profile["status"] == "unknown"
    assert profile["instructions"] == "none"


@pytest.mark.parametrize(
    "model",
    [
        "moss_ttsd_bf16_codec_f16",
        "moss_ttsd_q4_k_codec_f16",
        "moss_ttsd_q8_0_codec_f16",
    ],
)
def test_moss_ttsd_models_use_request_delivery_field_without_extra_effects(model: str):
    profile = capabilities_for_model(model, backend="audio_cpp", family="moss_ttsd")

    assert profile["status"] == "documented"
    assert profile["instructions"] == "field"
    assert profile["instruction_kind"] == "delivery"
    assert profile["instruction_scope"] == ["request"]
    assert profile["native_instruction_scope"] == ["request"]
    assert profile["instruction_transport"] == "options.instruct"
    assert profile["phrase_instruction_transport"] == "quoted_hint"
    assert profile["phrase_instruction_reliability"] == "best_effort"
    assert profile["emotion"] == {"mode": "none", "tags": []}
    assert profile["event_tags"] == {}
    assert profile["voice_design"] is False


def test_exact_legacy_gemini_variants_keep_route_specific_events():
    vertex31 = capabilities_for_model(
        "gemini-3.1-flash-tts-preview", backend="vertex_ai"
    )
    vertex25 = capabilities_for_model(
        "gemini-2.5-flash-preview-tts", backend="vertex_ai"
    )
    flash_lite = capabilities_for_model(
        "gemini-2.5-flash-lite-preview-tts", backend="vertex_ai"
    )

    assert vertex31["event_tags"] == {"sigh": "sigh", "laugh": "laughs"}
    assert vertex25["event_tags"] == {"sigh": "sigh"}
    assert flash_lite["status"] == "documented"
    assert flash_lite["dialect"] == "gemini"


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        ({"id": "openai", "provider": "openai", "name": "OpenAI"}, "openai"),
        (
            {
                "id": "custom-openai",
                "provider": "openai",
                "base_url": "https://api.openai.com/v1",
            },
            "openai",
        ),
        (
            {
                "id": "openai",
                "provider": "openai",
                "adapter": "openai_compatible",
                "api_base": "https://custom.example/v1",
            },
            "openai_compatible",
        ),
        (
            {
                "id": "openai",
                "provider": "openai",
                "adapter": "openai_compatible",
                "profile_id": "openai-compatible-preset",
                "base_url": "https://api.openai.com/v1",
            },
            "openai_compatible",
        ),
        (
            {
                "id": "azure-openai-v1",
                "provider": "openai",
                "adapter": "openai_compatible",
                "profile_id": "azure-openai-v1",
            },
            "azure_openai",
        ),
    ],
)
def test_capability_backend_requires_native_openai_identity(
    endpoint: dict[str, str], expected: str
):
    assert capability_backend(endpoint) == expected


@pytest.mark.parametrize(
    ("adapter", "expected"),
    [
        ("audio_cpp", "audio_cpp"),
        ("elevenlabs_native", "elevenlabs_native"),
        ("azure_speech", "azure_speech"),
    ],
)
def test_native_adapters_keep_route_priority(adapter: str, expected: str):
    assert capability_backend({"adapter": adapter, "provider": "openai"}) == expected


def test_compatible_openai_selector_does_not_gain_native_model_rules():
    endpoint = {
        "id": "openai",
        "provider": "openai",
        "adapter": "openai_compatible",
        "profile_id": "custom-openai",
        "api_base": "https://api.openai.com/v1",
    }
    route = capability_backend(endpoint)

    assert route == "openai_compatible"
    assert capabilities_for_model("tts-1", backend=route)["status"] == "unknown"
    assert capabilities_for_model("gpt-4o-mini-tts-fake", backend=route)["status"] == "unknown"


def test_azure_openai_requires_exact_upstream_model_mapping():
    azure_endpoint = {
        "id": "azure-openai-v1",
        "provider": "openai",
        "adapter": "openai_compatible",
        "model_catalog": [{"id": "deployment", "upstream_model_id": "gpt-4o-mini-tts"}],
    }
    mapped = capabilities_for_endpoint_model("deployment", azure_endpoint)
    unmapped = capabilities_for_endpoint_model(
        "gpt-4o-mini-tts",
        {**azure_endpoint, "model_catalog": [{"id": "gpt-4o-mini-tts"}]},
    )
    direct = capabilities_for_model("gpt-4o-mini-tts", backend="azure_openai")

    assert mapped["backend"] == "azure_openai"
    assert mapped["model"] == "deployment"
    assert mapped["status"] == "documented"
    assert unmapped["status"] == direct["status"] == "unknown"
