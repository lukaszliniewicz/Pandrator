"""Offline consistency checks for documented speech controls and request aliases."""

import pytest

from pandrator.logic.model_catalogue import catalogue_page
from pandrator.logic.speech_capabilities import (
    capabilities_for_model,
    capability_backend,
    decorate_service_capabilities,
)
from pandrator.logic.speech_performance import compile_performance, resolve_capabilities
from pandrator.logic.tts_handler import _build_audio_cpp_audio_payload, _build_voxcpm_payload
from pandrator.web.generation_rendering import build_render_parts


def _catalogued(provider: str, model: str) -> dict:
    return next(row for row in catalogue_page(provider=provider, query=model, limit=100)["items"] if row["id"] == model)


def _payload(model: str, **settings: object) -> dict:
    return _build_audio_cpp_audio_payload("Hello, world.", {"service": "audio_cpp", "model": model, **settings}, {})


def test_native_voxcpm_omits_unverified_directions_and_unused_speed():
    settings = {
        "service": "voxcpm", "xtts_model": "openbmb/VoxCPM2", "speed": 1.8,
        "generation_prompt": "Read warmly", "openai_audio_instructions": "Read sadly",
        "_performance": {"decision": "steer", "delivery": {"instruction": "Read slowly"}},
    }
    payload = _build_voxcpm_payload("  Hello 世界  ", settings)
    assert payload["input"] == "  Hello 世界  "
    assert "instructions" not in payload and "speed" not in payload
    assert payload["voxcpm"]["inference_timesteps"]
    assert settings["generation_prompt"] == "Read warmly"


@pytest.mark.parametrize("provider,model,transport", [
    ("gemini", "gemini-3.8-flash-tts", "speech_metadata.style"),
    ("vertex_ai", "gemini-3.8-flash-tts", "speechMetadata.style"),
])
def test_shared_route_profile_for_compatible_gemini_and_vertex(
    provider: str, model: str, transport: str,
):
    endpoint = {"id": provider, "provider": provider, "adapter": "openai_compatible", "models": [model]}
    assert capability_backend(endpoint) == provider
    decorate_service_capabilities(endpoint)
    advertised = endpoint["expressive_capabilities"][model]
    resolved = resolve_capabilities({"service": provider, "model": model}, endpoint)
    catalogued = _catalogued(provider, model)["expressive_capabilities"]
    for key in (
        "backend", "status", "instructions", "instruction_kind", "event_tags",
        "instruction_scope", "native_instruction_scope",
        "phrase_instruction_transport", "instruction_transport",
    ):
        assert advertised[key] == resolved[key] == catalogued[key]
    assert advertised["native_instruction_scope"] == ["request"]
    assert advertised["phrase_instruction_transport"] == "quoted_hint"
    assert advertised["instruction_transport"] == transport


@pytest.mark.parametrize("provider,transport", [
    ("gemini", "speech_metadata.style"),
    ("vertex_ai", "speechMetadata.style"),
])
def test_gemini38_compiler_keeps_request_span_scope_and_quoted_phrase_hint(
    provider: str, transport: str,
):
    model = "gemini-3.8-flash-tts"
    endpoint = {
        "id": provider,
        "provider": provider,
        "adapter": "openai_compatible",
        "models": [model],
    }
    text = "The clock stopped."
    compiled = compile_performance(
        text,
        {
            "service": provider,
            "model": model,
            "generation_prompt": "Read warmly",
            "_performance": {
                "decision": "steer",
                "spans": [{
                    "anchor": {"quote": "stopped"},
                    "delivery": {"instruction": "Emphasize this word"},
                }],
            },
        },
        endpoint,
    )
    assert compiled.transcript == compiled.input == text
    assert compiled.capabilities["instruction_scope"] == ["request", "span"]
    assert compiled.capabilities["native_instruction_scope"] == ["request"]
    assert compiled.capabilities["phrase_instruction_transport"] == "quoted_hint"
    assert compiled.capabilities["instruction_transport"] == transport
    assert "Read warmly" in compiled.instructions
    assert "For occurrence 1 of 'stopped': Emphasize this word." in compiled.instructions
    assert any(row["control"] == "span" and row["status"] == "approximated" for row in compiled.report)


def _compiled_markup(
    provider: str,
    model: str,
    text: str,
    markup: str,
    *,
    controls: dict | None = None,
    **settings: object,
):
    endpoint = {
        "id": provider,
        "provider": provider,
        "adapter": "openai_compatible",
        "models": [model],
    }
    parts = build_render_parts(
        text,
        {
            "service": provider,
            "model": model,
            "performance_enabled": True,
            **settings,
        },
        speech_xml=markup,
        segment_id="s",
        controls=controls,
    )
    return parts, compile_performance(parts[0]["text"], parts[0]["settings"], endpoint)


@pytest.mark.parametrize(
    "provider,model",
    [
        ("gemini", "gemini-3.8-flash-tts"),
        ("gemini", "gemini-3.8-flash-lite-tts"),
        ("vertex_ai", "gemini-3.8-flash-tts"),
        ("vertex_ai", "gemini-3.8-flash-lite-tts"),
    ],
)
def test_gemini38_markup_keeps_root_delivery_general_and_only_hints_local_scope(
    provider: str, model: str
):
    text = "The clock stopped."
    markup = (
        '<segment id="s"><ins>Whisper.</ins>The clock '
        '<span><ins>whisper this word</ins>stopped</span>.</segment>'
    )
    parts, compiled = _compiled_markup(provider, model, text, markup)
    assert len(parts) == 1
    assert compiled.transcript == compiled.input == text
    assert compiled.instructions == (
        "Whisper.\nFor occurrence 1 of 'stopped': whisper this word. "
        "Then resume the general delivery."
    )
    assert compiled.instructions.count("Whisper.") == 1
    span_reports = [
        row for row in compiled.report
        if row["control"] == "span" and row["status"] == "approximated"
    ]
    assert len(span_reports) == 1


def test_gemini38_root_only_and_root_with_local_emotion_are_differential():
    text = "The clock stopped."
    provider, model = "gemini", "gemini-3.8-flash-tts"
    root_parts, root_only = _compiled_markup(
        provider,
        model,
        text,
        '<segment id="s"><ins>Whisper.</ins>The clock stopped.</segment>',
    )
    assert root_parts[0]["settings"]["_performance_markup_scope"]["root_delivery"]["instruction"] == "Whisper."
    assert root_only.instructions == "Whisper."
    assert not any(row["control"] == "span" for row in root_only.report)

    _parts, local_emotion = _compiled_markup(
        provider,
        model,
        text,
        '<segment id="s"><ins>Whisper.</ins>The clock '
        '<span><em>sad</em>stopped</span>.</segment>',
    )
    assert local_emotion.transcript == local_emotion.input == text
    assert local_emotion.instructions == (
        "Whisper.\nFor occurrence 1 of 'stopped': sad tone. "
        "Then resume the general delivery."
    )
    assert local_emotion.instructions.count("Whisper.") == 1


def test_gemini38_instruction_patch_and_explicit_clear_only_override_selected_words():
    text = "The clock stopped then started."
    markup = (
        '<segment id="s"><ins>Whisper.</ins>The clock '
        '<span><ins>soften this word</ins>stopped</span> then '
        '<span><ins/>started</span>.</segment>'
    )
    _parts, compiled = _compiled_markup(
        "gemini", "gemini-3.8-flash-tts", text, markup
    )
    assert compiled.transcript == compiled.input == text
    assert compiled.instructions == (
        "Whisper.\n"
        "For occurrence 1 of 'stopped': soften this word. "
        "Then resume the general delivery.\n"
        "For occurrence 1 of 'started': Use natural delivery instead of the segment instruction. "
        "Then resume the general delivery."
    )
    assert compiled.instructions.count("Whisper.") == 1
    assert len([
        row for row in compiled.report
        if row["control"] == "span" and row["status"] == "approximated"
    ]) == 2


def test_gemini38_structured_resets_and_uniform_reset_do_not_repeat_root_style():
    text = "The clock stopped then started."
    _parts, structured = _compiled_markup(
        "gemini",
        "gemini-3.8-flash-tts",
        text,
        '<segment id="s"><em>sad</em><pace>slower</pace>The clock '
        '<span><em/><pace>natural</pace>stopped</span> then started.</segment>',
    )
    assert structured.instructions == (
        "sad tone; slightly slower pace\n"
        "For occurrence 1 of 'stopped': neutral tone; natural pace. "
        "Then resume the general delivery."
    )

    uniform_text = "The whole sentence."
    _parts, uniform_reset = _compiled_markup(
        "gemini",
        "gemini-3.8-flash-tts",
        uniform_text,
        '<segment id="s"><ins>Whisper.</ins>'
        '<span><ins/>The whole sentence.</span></segment>',
        generation_prompt="Project direction",
    )
    assert uniform_reset.transcript == uniform_reset.input == uniform_text
    assert uniform_reset.instructions == "Project direction"
    assert "Whisper." not in uniform_reset.instructions
    assert "For occurrence" not in uniform_reset.instructions


def test_gemini38_root_instruction_is_removed_once_after_character_default_prefix():
    text = "The clock stopped."
    controls = {
        "characters": [{
            "id": "c-one",
            "display_name": "One",
            "delivery": {"instruction": "Low register"},
        }],
        "cast": {},
    }
    _parts, compiled = _compiled_markup(
        "gemini",
        "gemini-3.8-flash-tts",
        text,
        '<segment id="s"><ins>Whisper.</ins>The clock '
        '<speaker ref="c-one"><ins>whisper this word</ins>stopped</speaker>.</segment>',
        controls=controls,
    )
    assert compiled.transcript == compiled.input == text
    assert compiled.instructions == (
        "Whisper.\nFor occurrence 1 of 'stopped': Low register\n"
        "whisper this word. Then resume the general delivery."
    )
    assert compiled.instructions.count("Whisper.") == 1


def test_gemini38_local_only_markup_and_legacy_json_keep_prior_projection():
    text = "The clock stopped."
    _parts, local_markup = _compiled_markup(
        "gemini",
        "gemini-3.8-flash-tts",
        text,
        '<segment id="s">The clock <span><ins>whisper this word</ins>'
        'stopped</span>.</segment>',
    )
    assert local_markup.instructions == (
        "For occurrence 1 of 'stopped': whisper this word. "
        "Then resume the general delivery."
    )

    endpoint = {
        "id": "gemini",
        "provider": "gemini",
        "adapter": "openai_compatible",
        "models": ["gemini-3.8-flash-tts"],
    }
    legacy = compile_performance(
        text,
        {
            "service": "gemini",
            "model": "gemini-3.8-flash-tts",
            "_performance": {
                "decision": "steer",
                "spans": [{
                    "anchor": {"quote": "stopped"},
                    "delivery": {"instruction": "whisper this word"},
                }],
            },
        },
        endpoint,
    )
    assert legacy.instructions == local_markup.instructions


@pytest.mark.parametrize(
    "provider,model",
    [
        ("gemini", "gemini-3.8-flash-experimental-unlisted"),
        ("audio_cpp", "qwen3_tts_1_7b_base_q8_0"),
    ],
)
def test_markup_scope_does_not_enable_instructions_for_unknown_or_qwen_clone(
    provider: str, model: str
):
    text = "Hello."
    settings = {"audio_cpp_voice_ref": {"sample_id": "local"}} if provider == "audio_cpp" else {}
    endpoint = {
        "id": provider,
        "provider": provider,
        "adapter": "audio_cpp" if provider == "audio_cpp" else "openai_compatible",
        "models": [model],
    }
    parts = build_render_parts(
        text,
        {"service": provider, "model": model, "performance_enabled": True, **settings},
        speech_xml='<segment id="s"><ins>Whisper.</ins>Hello.</segment>',
        segment_id="s",
    )
    compiled = compile_performance(parts[0]["text"], parts[0]["settings"], endpoint)
    assert compiled.transcript == compiled.input == text
    assert compiled.instructions == ""
    assert any(row["status"] == "unsupported" for row in compiled.report)


def test_gemini38_rejects_malformed_internal_markup_scope():
    with pytest.raises(ValueError, match="cover the transcript exactly"):
        compile_performance(
            "Hello",
            {
                "service": "gemini",
                "model": "gemini-3.8-flash-tts",
                "_performance_markup_scope": {
                    "root_delivery": {"instruction": "Whisper."},
                    "spans": [{
                        "start": 0,
                        "end": 4,
                        "delivery": {"instruction": "Whisper."},
                    }],
                },
            },
            {
                "id": "gemini",
                "provider": "gemini",
                "adapter": "openai_compatible",
                "models": ["gemini-3.8-flash-tts"],
            },
        )


@pytest.mark.parametrize("provider", ["gemini", "vertex_ai"])
def test_unrecognized_gemini_model_keeps_conservative_unknown_profile(provider: str):
    profile = capabilities_for_model(
        "gemini-3.8-flash-experimental-unlisted", backend=provider,
    )
    assert profile["status"] == "unknown"
    assert profile["dialect"] == "none"
    assert profile["instructions"] == "none"
    assert profile["instruction_scope"] == []


def test_firered_reference_mode_is_resolved_once_and_cache_separated():
    model = "fireredtts3_instruct_orig"
    endpoint = {"id": "audio_cpp", "adapter": "audio_cpp", "model_catalog": [{"id": model, "family": "fireredtts3"}]}
    settings = {"service": "audio_cpp", "model": model}
    cache = {}
    design = resolve_capabilities(settings, endpoint)
    clone = resolve_capabilities({**settings, "audio_cpp_voice_ref": {"sample_id": "local"}}, endpoint)
    assert design["instruction_kind"] == "voice_description"
    assert design["voice_design"] is True and design["uses_reference"] is False
    assert clone["instructions"] == "none" and clone["instruction_kind"] == "none"
    assert clone["voice_design"] is False and clone["uses_reference"] is True
    assert clone["emotion"]["mode"] == "none" and clone["instruction_scope"] == []
    assert capabilities_for_model(model, backend="audio_cpp", family="fireredtts3",
        voice_mode="optional_cloning", reference_audio=True) == clone
    # The normal cached endpoint path must still distinguish a linked reference.
    assert resolve_capabilities(settings, _service_config_cache=cache)["uses_reference"] is False
    assert resolve_capabilities({**settings, "audio_cpp_voice_ref": {"sample_id": "local"}}, _service_config_cache=cache)["uses_reference"] is True


def test_maya_requires_description_and_uses_verified_event_transport():
    model = "maya1_orig"
    with pytest.raises(ValueError, match="requires a voice description"):
        _payload(model)
    payload = _payload(model, generation_prompt="A gentle older narrator", performance_allow_vocalizations=True,
        _performance={"decision": "steer", "events": [{"kind": "laugh"}, {"kind": "cough"}]})
    assert payload["options"]["instruct"] == "A gentle older narrator"
    assert "instructions" not in payload
    assert "<laugh>" in payload["input"] and "<cough>" not in payload["input"]
    profile = _catalogued("audio_cpp", model)["expressive_capabilities"]
    assert profile["requires_instruction"] is True
    assert profile["event_tags"] == {"laugh": "laugh", "sigh": "sigh", "chuckle": "chuckle", "gasp": "gasp", "exhale": "exhale"}


def test_moss_instruction_aliases_are_compiler_owned():
    model = "moss_tts_v15_bf16_codec_f16"
    payload = _payload(model, generation_prompt="A low, husky voice", audio_cpp_options={
        "instruct": "stale", "instruction": "stale", "instructions": "stale", "caption": "stale"})
    assert payload["options"] == {"instruct": "A low, husky voice"}
    assert "instructions" not in payload
    assert _catalogued("audio_cpp", model)["expressive_capabilities"]["voice_design"] is False


@pytest.mark.parametrize("model,supported", [
    ("irodori_tts_500m_v3_f16", False),
    ("irodori_tts_500m_v3_q8_0", False),
    ("irodori_tts_600m_v3_voicedesign_f16", True),
    ("irodori_tts_v4_small_f16", True),
    ("irodori_tts_v4_1_anime_q8_0", False),
])
def test_irodori_exact_variants_and_reference_behavior(model: str, supported: bool):
    profile = capabilities_for_model(model, backend="audio_cpp", family="irodori_tts")
    assert (profile["instructions"] == "field") is supported
    if "anime" in model:
        assert profile["status"] == "unknown"
        return
    settings = {"generation_prompt": "A warm voice"} if supported else {}
    payload = _payload(model, **settings)
    assert payload["options"]["no_ref"] is True
    if supported:
        assert payload["options"]["instruction"] == "A warm voice"
    else:
        assert "instruction" not in payload["options"]
    linked = _payload(model, audio_cpp_voice_ref={"sample_id": "local"}, audio_cpp_options={"no_ref": True}, **settings)
    assert linked["options"]["no_ref"] is False
    assert linked["voice_ref"] == {"sample_id": "local"}


def test_voice_description_omits_phrase_directions_and_reports_approximation():
    model = "moss_voicegen_bf16_codec_f16_decode"
    annotation = {"decision": "steer", "delivery": {"pace": "slower"},
        "spans": [{"anchor": {"quote": "world"}, "delivery": {"instruction": "whisper this word"}}]}
    compiled = compile_performance("Hello, world.", {"service": "audio_cpp", "model": model,
        "generation_prompt": "An elderly narrator", "_performance": annotation}, {"adapter": "audio_cpp"})
    assert compiled.input == compiled.transcript == "Hello, world."
    assert "whisper this word" not in compiled.instructions
    assert "An elderly narrator" in compiled.instructions
    assert any(row["control"] == "direction" and row["status"] == "approximated" for row in compiled.report)
    assert any(row["control"] == "span" and row["status"] == "unsupported" for row in compiled.report)


def test_qwen_clone_directions_stay_saved_but_uncompiled():
    model = "qwen3_tts_1_7b_base_q8_0"
    compiled = compile_performance("Literal <provider> text", {"service": "audio_cpp", "model": model,
        "generation_prompt": "Whisper", "_performance": {"decision": "steer", "spans": [
            {"anchor": {"quote": "Literal"}, "delivery": {"emotion": "sad"}}]}}, {"adapter": "audio_cpp"})
    assert compiled.input == "Literal <provider> text"
    assert compiled.instructions == ""
    assert any(row["status"] == "unsupported" for row in compiled.report)


@pytest.mark.parametrize("model", ["auk_qwen_bf16", "auk_qwen_q8_0", "auk_vae_f32", "vocos_mel_24khz"])
def test_component_rejected_even_if_legacy_endpoint_claims_prebuilt(model: str):
    endpoint = {"model_catalog": [{"id": model, "voice_mode": "prebuilt"}], "model_voice_modes": {model: "prebuilt"}}
    with pytest.raises(ValueError, match="component"):
        _build_audio_cpp_audio_payload("Hello", {"model": model}, endpoint)
    assert not any(row["id"] == model for row in catalogue_page(provider="audio_cpp", query=model, limit=100)["items"])
