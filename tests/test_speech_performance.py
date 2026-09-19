"""pSSML safety, model routing and semantic-context compilation regressions."""

import pytest

from pandrator.logic.speech_performance import (
    PerformanceAnnotation,
    capabilities_for_model,
    compile_performance,
    validate_annotation,
)
from pandrator.logic import tts_handler
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
