"""pSSML safety, model routing and semantic-context compilation regressions."""

import pytest

from pandrator.logic import tts_handler
from pandrator.logic.speech_performance import (
    PerformanceAnnotation,
    capabilities_for_model,
    compile_performance,
    decorate_service_capabilities,
    resolve_capabilities,
    validate_annotation,
)
from pandrator.web.speech_plan_workspace import semantic_context_window


def settings(model="fish_audio_s2_pro_q8_0", **overrides):
    return {
        "service": "audio_cpp",
        "model": model,
        "performance_enabled": True,
        **overrides,
    }


def steer(instruction="Measured contrast", **extra):
    return {"decision": "steer", "delivery": {"instruction": instruction}, **extra}


@pytest.mark.parametrize(
    "model,route,supported",
    [
        ("qwen3_tts_1_7b_base_q8_0", "audio_cpp", False),
        ("qwen3_tts_0_6b_customvoice_q8_0", "audio_cpp", False),
        ("qwen3_tts_1_7b_customvoice_q8_0", "audio_cpp", True),
        ("qwen3_tts_1_7b_voicedesign_q8_0", "audio_cpp", True),
        ("gemini-2.5-flash-tts", "gemini", True),
        ("gemini-3.1-flash-tts-preview", "vertex_ai", True),
        ("gpt-4o-mini-tts", "openai", True),
        ("tts-1-hd", "openai", False),
        ("fish_audio_s2_pro_q8_0", "audio_cpp", True),
        ("unrecognized", "unrecognized", False),
    ],
)
def test_capability_is_model_and_route_specific(model, route, supported):
    profile = capabilities_for_model(model, backend=route)
    assert (profile["instructions"] != "none") is supported


def test_elevenlabs_profiles_are_exact_model_and_route_intersections():
    v3 = capabilities_for_model("eleven_v3", backend="elevenlabs_native")
    assert v3["instructions"] == "inline"
    assert v3["instruction_scope"] == ["request", "span"]
    assert v3["emotion"]["mode"] == "open_description"
    assert v3["event_tags"] == {
        "laugh": "laughs", "sigh": "sighs", "clear_throat": "clears throat"
    }
    assert v3["voice_design"] is False
    assert v3["semantic_context"] == "none"
    assert any("may be ignored" in note for note in v3["notes"])
    assert capabilities_for_model("eleven_v3", backend="elevenlabs")["instructions"] == "inline"
    assert capabilities_for_model("eleven_v3", backend="custom")["instructions"] == "none"
    assert capabilities_for_model("eleven_v3_preview", backend="elevenlabs")["instructions"] == "none"
    for model in (
        "eleven_multilingual_v2", "eleven_flash_v2", "eleven_flash_v2_5",
        "eleven_turbo_v2", "eleven_turbo_v2_5",
    ):
        profile = capabilities_for_model(model, backend="elevenlabs_native")
        assert profile["semantic_context"] == "field"
        assert profile["instructions"] == "none"
    assert capabilities_for_model("future_eleven", backend="elevenlabs_native")["semantic_context"] == "none"
    custom = {"id": "custom", "adapter": "elevenlabs_native", "models": ["eleven_v3"]}
    decorate_service_capabilities(custom)
    assert custom["expressive_capabilities"]["eleven_v3"]["instructions"] == "inline"


def test_eleven_v3_inline_scope_and_event_gating_preserve_transcript():
    text = "Hello again."
    options = {
        "service": "elevenlabs", "model": "eleven_v3",
        "generation_prompt": "Measured narration",
        "_performance": steer(
            spans=[{"anchor": {"quote": "again"}, "delivery": {"emotion": "wistful"}}],
            events=[{"kind": "laugh", "position": "after", "anchor": {"quote": "Hello"}},
                    {"kind": "clear_throat", "position": "before"},
                    {"kind": "chuckle", "position": "after"}],
        ),
    }
    disabled = compile_performance(text, options, {"adapter": "elevenlabs_native", "id": "custom"})
    assert disabled.transcript == text
    assert disabled.input.startswith("[Measured narration; Measured contrast]")
    assert "[wistful tone]again[Measured narration; Measured contrast]" in disabled.input
    assert "[laughs]" not in disabled.input
    assert disabled.instructions == ""
    enabled = compile_performance(
        text, {**options, "performance_allow_vocalizations": True},
        {"adapter": "elevenlabs_native", "id": "custom"},
    )
    assert "[clears throat]" in enabled.input
    assert "[laughs]" in enabled.input
    assert "[chuckle]" not in enabled.input
    assert any(item["control"] == "span" and item["status"] == "approximated" for item in enabled.report)
    assert any(item["control"] == "event" and item["status"] == "unsupported" for item in enabled.report)


def test_eleven_v3_rejects_nested_directions_and_excludes_stitching():
    options = {
        "service": "elevenlabs", "model": "eleven_v3",
        "tts_context_mode": "both",
        "_semantic_context": {"before": "Earlier.", "after": "Later."},
    }
    compiled = compile_performance("Now.", options, {"adapter": "elevenlabs_native"})
    assert compiled.input == "Now."
    assert compiled.request_options == {}
    assert any(item["control"] == "semantic_context" and item["status"] == "unsupported" for item in compiled.report)
    with pytest.raises(ValueError, match="provider tags or control tokens"):
        compile_performance("Now.", {**options, "generation_prompt": "[whisper]"}, {"adapter": "elevenlabs_native"})


@pytest.mark.parametrize(
    "mode,expected", [("off", {}), ("before", {"previous_text": "Earlier."}),
                      ("both", {"previous_text": "Earlier.", "next_text": "Later."})],
)
def test_eleven_v2_context_compiles_into_fingerprinted_fields(mode, expected):
    options = {
        "service": "elevenlabs", "model": "eleven_multilingual_v2",
        "tts_context_mode": mode,
        "_semantic_context": {"before": "Earlier.", "after": "Later."},
        "generation_prompt": "Never speak this.",
    }
    compiled = compile_performance("Now.", options, {"adapter": "elevenlabs_native"})
    assert compiled.input == compiled.transcript == "Now."
    assert compiled.request_options == expected
    assert compiled.instructions == ""
    assert any(item["control"] == "direction" and item["status"] == "unsupported" for item in compiled.report)
    if mode != "off":
        assert compiled.fingerprint != compile_performance(
            "Now.", {**options, "tts_context_mode": "off"}, {"adapter": "elevenlabs_native"}
        ).fingerprint


def test_resolved_capability_cache_is_input_complete_and_mutation_isolated(monkeypatch):
    calls = 0
    original = tts_handler.get_service_config

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(tts_handler, "get_service_config", counted)
    cache = {}
    base = settings("fireredtts3_base_q8_0")
    first = resolve_capabilities(base, _service_config_cache=cache)
    first["notes"].append("caller mutation")
    assert resolve_capabilities(base, _service_config_cache=cache) == resolve_capabilities(base)
    assert "caller mutation" not in resolve_capabilities(base, _service_config_cache=cache)["notes"]
    assert calls == 2  # one cached computation and one uncached comparison

    variants = [
        {**base, "model": "unrecognized"},
        {**base, "audio_cpp_voice_ref": {"type": "base64", "data": "example"}},
        {**base, "service_configs": [{"id": "audio_cpp", "backend_version": "changed"}]},
    ]
    for variant in variants:
        before = calls
        cached = resolve_capabilities(variant, _service_config_cache=cache)
        assert calls == before + 1
        assert cached == resolve_capabilities(variant)
        assert resolve_capabilities(variant, _service_config_cache=cache) == cached
    assert resolve_capabilities(variants[1], _service_config_cache=cache)["instructions"] == "none"
    reference_profile = resolve_capabilities(variants[1], _service_config_cache=cache)
    assert reference_profile["instruction_scope"] == []
    assert reference_profile["emotion"] == {"mode": "none", "tags": []}


def test_explicit_endpoint_bypasses_resolved_capability_cache():
    cache = {}
    base = {"service": "openai", "model": "gpt-4o-mini-tts"}
    default = resolve_capabilities(base, _service_config_cache=cache)
    explicit = resolve_capabilities(
        base,
        {"id": "custom", "provider": "custom", "backend_version": "different"},
        _service_config_cache=cache,
    )
    assert explicit == resolve_capabilities(
        base, {"id": "custom", "provider": "custom", "backend_version": "different"}
    )
    assert resolve_capabilities(base, _service_config_cache=cache) == default


def test_fish_compiles_inline_without_changing_transcript():
    text = "しかし、それは一時的でした。"
    annotation = steer(
        spans=[{"anchor": {"quote": "一時的"}, "delivery": {"emphasis": "moderate"}}]
    )
    compiled = compile_performance(text, settings(_performance=annotation))
    assert compiled.transcript == text
    assert "[moderate emphasis]一時的" in compiled.input
    assert not compiled.instructions
    assert any(item["status"] == "approximated" for item in compiled.report)


def test_qwen_compiles_one_global_instruction_not_more_segments():
    annotation = steer(
        spans=[{"anchor": {"quote": "temporary"}, "delivery": {"emphasis": "light"}}]
    )
    compiled = compile_performance(
        "This defeat is temporary.",
        settings("qwen3_tts_1_7b_customvoice_q8_0", _performance=annotation),
    )
    assert compiled.input == compiled.transcript
    assert "temporary" in compiled.instructions
    assert any(
        item["control"] == "span" and item["status"] == "approximated"
        for item in compiled.report
    )


def test_gemini_context_is_separate_and_transcript_is_last():
    text = "Yet the defeat was temporary."
    compiled = compile_performance(
        text,
        {
            "service": "gemini",
            "model": "gemini-2.5-flash-tts",
            "generation_prompt": "Restrained narration",
            "tts_context_mode": "both",
            "_semantic_context": {
                "before": "The request failed.",
                "after": "Membership doubled.",
            },
        },
    )
    assert compiled.input.endswith("Transcript:\n" + text)
    assert "Following context (do not speak):" in compiled.input
    assert "Speaking directions:\nRestrained narration" in compiled.input
    assert compiled.transcript == text
    assert not compiled.instructions


def test_gemini_context_only_is_not_dependent_on_direction():
    compiled = compile_performance(
        "Yes.",
        {
            "service": "gemini",
            "model": "gemini-2.5-flash-tts",
            "tts_context_mode": "before",
            "_semantic_context": {
                "before": "Was the request accepted?",
                "after": "Never speak this.",
            },
        },
    )
    assert "Was the request accepted?" in compiled.input
    assert "Never speak this." not in compiled.input


def test_unsupported_instructions_never_become_spoken_text():
    compiled = compile_performance(
        "Current text.",
        settings(
            "qwen3_tts_1_7b_base_q8_0",
            generation_prompt="Do not read this direction.",
            _performance=steer(),
            tts_context_mode="both",
            _semantic_context={"before": "Do not read context."},
        ),
    )
    assert compiled.input == "Current text."
    assert compiled.instructions == ""
    assert {
        item["control"] for item in compiled.report if item["status"] == "unsupported"
    } == {"direction", "semantic_context"}


@pytest.mark.parametrize(
    "annotation",
    [
        {"decision": "none", "delivery": {"instruction": "Whisper"}},
        {"decision": "steer", "text": "A rewrite"},
        steer("[whisper]"),
        steer(
            spans=[{"anchor": {"quote": "absent"}, "delivery": {"emphasis": "light"}}]
        ),
        steer(spans=[{"anchor": {"quote": "yes"}, "delivery": {"emphasis": "light"}}]),
        steer(
            spans=[
                {
                    "anchor": {"quote": "yes", "occurrence": True},
                    "delivery": {"emphasis": "light"},
                }
            ]
        ),
        steer(
            spans=[
                {
                    "anchor": {"quote": "yes", "occurrence": 1},
                    "delivery": {"emphasis": "light"},
                },
                {"anchor": {"quote": "yes yes"}, "delivery": {"emphasis": "strong"}},
            ]
        ),
    ],
)
def test_invalid_or_ambiguous_annotations_are_rejected(annotation):
    with pytest.raises(ValueError):
        validate_annotation("yes yes", annotation)


def test_repeated_anchor_with_occurrence_is_supported():
    validate_annotation(
        "yes yes",
        steer(
            spans=[
                {
                    "anchor": {"quote": "yes", "occurrence": 2},
                    "delivery": {"emphasis": "light"},
                }
            ]
        ),
    )


def test_graphemes_cannot_be_split():
    with pytest.raises(ValueError, match="Unicode"):
        validate_annotation(
            "e\u0301",
            steer(
                spans=[{"anchor": {"quote": "e"}, "delivery": {"emphasis": "light"}}]
            ),
        )


def test_events_require_opt_in_and_do_not_affect_alignment_text():
    annotation = steer(events=[{"kind": "sigh"}])
    disabled = compile_performance("Text", settings(_performance=annotation))
    enabled = compile_performance(
        "Text", settings(_performance=annotation, performance_allow_vocalizations=True)
    )
    assert "[sigh]" not in disabled.input
    assert "[sigh]" in enabled.input
    assert disabled.transcript == enabled.transcript == "Text"


def test_reason_confidence_and_locks_do_not_invalidate_audio():
    one = compile_performance(
        "Text", settings(_performance=steer(reason="One", locked=False))
    )
    two = compile_performance(
        "Text",
        settings(_performance=steer(reason="Two", locked=True, confidence="low")),
    )
    assert one.fingerprint == two.fingerprint


def test_disabled_performance_leaves_general_direction_working():
    result = compile_performance(
        "Text",
        settings(
            performance_enabled=False,
            generation_prompt="Calm",
            _performance=steer("Not applied"),
        ),
    )
    assert result.input == "[Calm]Text"


def test_context_zero_boundaries_and_shared_source_units():
    units = [
        {"id": str(i), "text": text, "speaker": speaker, "node_kind": "paragraph"}
        for i, (text, speaker) in enumerate(
            [("Old", "A"), ("Middle", "A"), ("New", "B")]
        )
    ]
    result = semantic_context_window(
        units, {"performance_context_before": 0, "performance_context_after": 0}
    )
    assert all(value == {"before": "", "after": ""} for value in result.values())
    result = semantic_context_window(units, {"performance_context_max_chars": 0})
    assert all(value == {"before": "", "after": ""} for value in result.values())
    assert semantic_context_window(units, {})["1"] == {
        "before": "Old",
        "after": "[Other speaker: B] New",
    }
    assert (
        "[Other speaker: A] Middle" in semantic_context_window(units, {})["2"]["before"]
    )
    assert set(semantic_context_window(units, {}, target_ids={"1"})) == {"1"}


def test_http_payload_compilation_uses_clean_text_and_correct_field():
    text = "Current utterance."
    fish = tts_handler._build_audio_cpp_audio_payload(
        text, settings(generation_prompt="Reflective"), {}
    )
    assert fish["input"] == "[Reflective]" + text
    assert "instructions" not in fish
    assert fish["options"]["text_chunk_mode"] == "tag_aware"
    plain_fish = tts_handler._build_audio_cpp_audio_payload(text, settings(), {})
    assert "text_chunk_mode" not in plain_fish.get("options", {})
    qwen = tts_handler._build_audio_cpp_audio_payload(
        text,
        settings("qwen3_tts_1_7b_customvoice_q8_0", generation_prompt="Reflective"),
        {},
    )
    assert qwen["input"] == text and qwen["instructions"] == "Reflective"
    base = tts_handler._build_audio_cpp_audio_payload(
        text, settings("qwen3_tts_1_7b_base_q8_0", generation_prompt="Reflective"), {}
    )
    assert base["input"] == text and "instructions" not in base


def test_schema_roundtrip_and_unknown_version_rejection():
    value = validate_annotation("Text", {})
    assert value["schema"] == "pandrator.performance/v1"
    PerformanceAnnotation.model_validate(value)
    with pytest.raises(ValueError):
        PerformanceAnnotation.model_validate({"schema": "unrecognized"})
