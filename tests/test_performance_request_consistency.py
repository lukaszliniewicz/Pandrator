"""Preview context and provider request-size consistency regressions."""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from pandrator.logic.speech_performance import compile_performance
from pandrator_mcp.schemas.performance import PreviewPerformancePlanInput
from pandrator.web import performance_plans
from pandrator.web.performance_schemas import PerformancePreviewRequest
from pandrator.web.speech_plan_workspace import semantic_context_window


def _vertex_settings(**overrides):
    return {
        "service": "vertex_ai",
        "model": "gemini-2.5-flash-tts",
        **overrides,
    }


def test_preview_uses_effective_runtime_context_not_historical_plan_settings(
    monkeypatch,
):
    plan = SimpleNamespace(
        id="plan",
        settings_json={
            "context_before": 0,
            "context_after": 0,
            "context_max_chars": 0,
        },
        units_json=[
            {
                "id": "before",
                "text": "Historical context.",
                "spoken_text": "Historical context.",
                "speaker": "A",
                "language": "en",
                "node_kind": "paragraph",
                "section_id": "section",
            },
            {
                "id": "target",
                "text": "Current utterance.",
                "spoken_text": "Current utterance.",
                "speaker": "A",
                "language": "en",
                "node_kind": "paragraph",
                "section_id": "section",
            },
        ],
    )
    monkeypatch.setattr(performance_plans, "_assert_current", lambda *_args: None)
    monkeypatch.setattr(
        performance_plans,
        "_annotations",
        lambda *_args: {"target": {"decision": "none"}},
    )

    preview = performance_plans.preview_segment(
        None,
        plan,
        "target",
        {
            "service": "gemini",
            "model": "gemini-2.5-flash-tts",
            "performance_context_before": 1,
            "performance_context_after": 0,
            "performance_context_max_chars": 200,
            "tts_context_mode": "both",
        },
    )

    assert "Historical context." in preview["input"]


@pytest.mark.parametrize("model", [PerformancePreviewRequest, PreviewPerformancePlanInput])
def test_preview_context_overrides_are_optional_and_bounded(model):
    common = {"segment_id": "segment"}
    if model is PreviewPerformancePlanInput:
        common.update(session_id="session", plan_id="plan")
    value = model.model_validate(common)
    assert value.context_before is None
    assert value.context_after is None
    assert value.context_max_chars is None
    with pytest.raises(ValidationError):
        model.model_validate({**common, "context_before": 21})
    with pytest.raises(ValidationError):
        model.model_validate({**common, "context_max_chars": 16001})


def test_context_limits_are_zero_safe_and_preserve_marked_graphemes():
    units = [
        {
            "id": "other",
            "text": "e\u0301" * 100,
            "speaker": "Other",
            "language": "en",
            "node_kind": "paragraph",
            "section_id": "section",
        },
        {
            "id": "target",
            "text": "Yes.",
            "speaker": "Current",
            "language": "en",
            "node_kind": "paragraph",
            "section_id": "section",
        },
    ]
    empty = semantic_context_window(
        units,
        {
            "performance_context_before": 0,
            "performance_context_after": 0,
            "performance_context_max_chars": 0,
        },
    )
    assert empty["target"] == {"before": "", "after": ""}

    context = semantic_context_window(
        units,
        {
            "performance_context_before": 1,
            "performance_context_after": 0,
            "performance_context_max_chars": 45,
        },
    )["target"]["before"]
    assert context.startswith("[Other speaker: Other] ")
    assert len(context) <= 45
    assert context.endswith("e\u0301")


def test_vertex_reduces_optional_context_but_preserves_required_content():
    text = "Current word."
    compiled = compile_performance(
        text,
        _vertex_settings(
            generation_prompt="Read this with measured clarity.",
            tts_context_mode="both",
            _performance={
                "decision": "steer",
                "spans": [
                    {
                        "anchor": {"quote": "word"},
                        "delivery": {"emphasis": "moderate"},
                    }
                ],
            },
            _semantic_context={
                "before": "[Other speaker: Narrator] " + "前" * 7000,
                "after": "後" * 7000,
            },
        ),
        {"id": "vertex_ai"},
    )

    assert len(compiled.input.encode("utf-8")) + len(
        compiled.instructions.encode("utf-8")
    ) <= 8000
    assert compiled.transcript == text
    assert "Read this with measured clarity." in compiled.input
    assert "[moderate emphasis]" in compiled.input
    assert "[Other speaker: Narrator]" in compiled.input
    assert any(
        item["status"] == "approximated"
        and item["control"] == "semantic_context"
        and "8000" in item["message"]
        for item in compiled.report
    )


@pytest.mark.parametrize("character", ["A", "語"])
def test_vertex_byte_boundary_counts_utf8_bytes(character):
    compiled = compile_performance(
        "話す。",
        _vertex_settings(
            tts_context_mode="before",
            _semantic_context={"before": character * 10000},
        ),
        {"id": "vertex_ai"},
    )
    assert len(compiled.input.encode("utf-8")) <= 8000


def test_vertex_required_transcript_and_directions_can_fail_before_http():
    with pytest.raises(ValueError, match="8000"):
        compile_performance(
            "x" * 7900,
            _vertex_settings(generation_prompt="A required direction."),
            {"id": "vertex_ai"},
        )


def test_unknown_route_has_no_vertex_byte_cap():
    text = "x" * 9000
    compiled = compile_performance(
        text,
        {"service": "future_tts", "model": "future-model"},
        {"id": "future_tts"},
    )
    assert compiled.transcript == text
    assert len(compiled.input) == len(text)
