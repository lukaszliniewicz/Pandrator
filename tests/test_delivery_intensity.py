from __future__ import annotations

import math

import pytest

from pandrator.logic.speech_capabilities import capabilities_for_endpoint_model
from pandrator.logic.speech_markup import parse_speech_markup
from pandrator.logic.speech_markup_edits import edit_speech_markup_range
from pandrator.logic.speech_performance import (
    Delivery,
    compile_performance,
    validate_annotation,
)
from pandrator.web import models as m
from pandrator.web import speech_selection
from pandrator.web.generation_controls import save_generation_controls
from pandrator.web.speech_selection import (
    apply_speech_selection,
    get_speech_selection_context,
    preview_speech_selection,
)
from pandrator.web.speech_selection_schemas import SpeechSelectionDelivery
from pandrator_mcp.schemas.audiobook import SpeechSelectionDeliveryInput
from tests.test_performance_plans import case as case  # noqa: PLC0414

AZURE_MODEL = "MAI-Voice-2.1"
AZURE_VOICE = f"en-US-Ethan:{AZURE_MODEL}"
AZURE_ENDPOINT = {
    "id": "azure-speech-intensity-test",
    "adapter": "azure_speech",
    "provider": "azure",
    "default_model": AZURE_MODEL,
    "default_voice": AZURE_VOICE,
}


def _index(delivery: dict, *, enabled: bool = True):
    return compile_performance(
        "Hello.",
        {
            "service": "audio_cpp",
            "model": "index_tts2_q8_0",
            "performance_enabled": enabled,
            "_performance": {"decision": "steer", "delivery": delivery},
        },
        {"id": "audio_cpp", "adapter": "audio_cpp"},
    )


def _markup_scope(xml: str, text: str) -> dict:
    parsed = parse_speech_markup(
        xml, expected_segment_id="s", expected_text=text
    )
    return {
        "root_delivery": parsed.root_delivery,
        "spans": [
            {"start": span.start, "end": span.end, "delivery": span.delivery}
            for span in parsed.spans
        ],
    }


def _azure(delivery: dict, *, voice: str = AZURE_VOICE):
    return compile_performance(
        "Hello.",
        {
            "service": AZURE_ENDPOINT["id"],
            "model": AZURE_MODEL,
            "voice": voice,
            "_performance": {"decision": "steer", "delivery": delivery},
        },
        AZURE_ENDPOINT,
    )


def test_delivery_intensity_is_optional_finite_bounded_and_requires_emotion():
    assert Delivery().emotion_intensity is None
    for value in (0, 0.5, 1, 2):
        parsed = Delivery.model_validate(
            {"emotion": "frustrated", "emotion_intensity": value}
        )
        assert parsed.emotion_intensity == value
    for value in (-0.01, 2.01, math.nan, math.inf, -math.inf):
        with pytest.raises(ValueError):
            Delivery.model_validate(
                {"emotion": "frustrated", "emotion_intensity": value}
            )
    with pytest.raises(ValueError, match="requires a non-empty emotion"):
        Delivery.model_validate({"emotion_intensity": 0})
    with pytest.raises(ValueError, match="requires a non-empty emotion"):
        Delivery.model_validate({"emotion": "  ", "emotion_intensity": 0.4})


def test_selection_delivery_schemas_accept_zero_and_reject_nonfinite_values():
    for schema in (SpeechSelectionDelivery, SpeechSelectionDeliveryInput):
        assert schema.model_validate(
            {"emotion": "sad", "emotion_intensity": 0}
        ).emotion_intensity == 0
        with pytest.raises(ValueError):
            schema.model_validate({"emotion_intensity": math.inf})
        with pytest.raises(ValueError):
            schema.model_validate({"emotion_intensity": 2.01})


def test_index_tts2_exposes_only_its_native_request_intensity_metadata():
    profile = capabilities_for_endpoint_model(
        "index_tts2_q8_0",
        {"id": "audio_cpp", "adapter": "audio_cpp"},
        family="index_tts2",
    )
    assert profile["emotion"]["intensity"] == {
        "min": 0.0,
        "max": 1.0,
        "default": 1.0,
        "transport": "options.emotion_alpha",
        "scope": "request",
        "label": "Emotion intensity",
        "semantics": profile["emotion"]["intensity"]["semantics"],
    }
    assert "IndexTTS2" in profile["emotion"]["intensity"]["semantics"]
    assert "default" not in capabilities_for_endpoint_model(
        "neutts_2e_orig",
        {"id": "audio_cpp", "adapter": "audio_cpp"},
        family="neutts",
    )["emotion"]


@pytest.mark.parametrize("intensity", [0.0, 0.5, 1.0])
def test_index_tts2_emits_explicit_intensity_values_including_zero(intensity):
    compiled = _index(
        {"emotion": "quiet delight", "emotion_intensity": intensity}
    )
    assert compiled.request_options == {
        "emotion_text": "quiet delight",
        "use_emotion_text": True,
        "emotion_alpha": intensity,
    }
    assert any(
        row["status"] == "applied" and row["control"] == "emotion_intensity"
        for row in compiled.report
    )


def test_native_intensity_absence_bounds_and_disabled_performance_do_not_add_defaults():
    absent = _index({"emotion": "quiet delight"})
    assert absent.request_options == {
        "emotion_text": "quiet delight",
        "use_emotion_text": True,
    }

    outside_native_range = _index(
        {"emotion": "quiet delight", "emotion_intensity": 1.1}
    )
    assert "emotion_alpha" not in outside_native_range.request_options
    assert any(
        row["status"] == "unsupported"
        and row["control"] == "emotion_intensity"
        and "retained" in row["message"]
        for row in outside_native_range.report
    )
    retained = validate_annotation(
        "Hello.",
        {
            "decision": "steer",
            "delivery": {"emotion": "quiet delight", "emotion_intensity": 1.1},
        },
    )
    assert retained["delivery"]["emotion_intensity"] == 1.1

    disabled = _index(
        {"emotion": "quiet delight", "emotion_intensity": 0.5}, enabled=False
    )
    assert disabled.request_options == {}
    assert not any(row["control"] == "emotion_intensity" for row in disabled.report)


@pytest.mark.parametrize("intensity", [0.01, 1.0, 2.0])
def test_azure_selected_voice_emits_documented_style_degree(intensity):
    profile = capabilities_for_endpoint_model(
        AZURE_MODEL, AZURE_ENDPOINT, voice=AZURE_VOICE
    )
    assert profile["emotion"]["intensity"] == {
        "min": 0.01,
        "max": 2.0,
        "default": 1.0,
        "transport": "options.azure_speech_style_degree",
        "scope": "request",
        "label": "Style degree",
        "semantics": profile["emotion"]["intensity"]["semantics"],
    }
    assert "speaking style" in profile["emotion"]["intensity"]["semantics"]
    compiled = _azure({"emotion": "happy", "emotion_intensity": intensity})
    assert compiled.request_options == {
        "azure_speech_style": "happy",
        "azure_speech_style_degree": intensity,
    }


def test_azure_style_degree_requires_an_exact_supported_selected_voice_style():
    no_voice = capabilities_for_endpoint_model(AZURE_MODEL, AZURE_ENDPOINT)
    unknown_voice = capabilities_for_endpoint_model(
        AZURE_MODEL,
        AZURE_ENDPOINT,
        voice=f"unlisted:{AZURE_MODEL}",
    )
    assert "intensity" not in no_voice["emotion"]
    assert "intensity" not in unknown_voice["emotion"]

    compiled = _azure(
        {"emotion": "educational", "emotion_intensity": 1.0}
    )
    assert compiled.request_options == {}
    assert any(row["control"] == "emotion" for row in compiled.report)
    assert any(
        row["status"] == "unsupported" and row["control"] == "emotion_intensity"
        for row in compiled.report
    )

    below_native_minimum = _azure(
        {"emotion": "happy", "emotion_intensity": 0.0}
    )
    assert below_native_minimum.request_options == {"azure_speech_style": "happy"}
    assert any(
        row["status"] == "unsupported" and row["control"] == "emotion_intensity"
        for row in below_native_minimum.report
    )


def test_unsupported_model_retains_intensity_without_emitting_it_as_prose_or_option():
    compiled = compile_performance(
        "Hello.",
        {
            "service": "openai",
            "model": "tts-1",
            "_performance": {
                "decision": "steer",
                "delivery": {"emotion": "sad", "emotion_intensity": 0.5},
            },
        },
        {"id": "openai", "provider": "openai"},
    )
    assert compiled.request_options == {}
    assert "0.5" not in compiled.input + compiled.instructions
    retained = validate_annotation(
        "Hello.",
        {
            "decision": "steer",
            "delivery": {"emotion": "sad", "emotion_intensity": 0.5},
        },
    )
    assert retained["delivery"]["emotion_intensity"] == 0.5
    assert any(
        row["status"] == "unsupported" and row["control"] == "emotion_intensity"
        for row in compiled.report
    )


def test_uniform_markup_applies_root_intensity_and_differing_phrase_values_stay_local():
    text = "First. Second."
    uniform = _markup_scope(
        '<segment id="s"><em intensity="0.5">happy</em>First. Second.</segment>',
        text,
    )
    compiled = compile_performance(
        text,
        {"service": "audio_cpp", "model": "index_tts2_q8_0", "_performance_markup_scope": uniform},
        {"id": "audio_cpp", "adapter": "audio_cpp"},
    )
    assert compiled.request_options["emotion_alpha"] == 0.5

    phrase_xml = (
        '<segment id="s"><em intensity="0.5">happy</em>First. '
        '<span><em intensity="0.8">sad</em>Second.</span></segment>'
    )
    scoped = _markup_scope(phrase_xml, text)
    compiled = compile_performance(
        text,
        {"service": "audio_cpp", "model": "index_tts2_q8_0", "_performance_markup_scope": scoped},
        {"id": "audio_cpp", "adapter": "audio_cpp"},
    )
    assert compiled.request_options["emotion_text"] == "happy"
    assert compiled.request_options["emotion_alpha"] == 0.5
    assert compiled.request_options["emotion_alpha"] != 0.8
    assert any(
        row["status"] == "unsupported" and row["control"] == "emotion"
        for row in compiled.report
    )
    assert any(
        row["status"] == "unsupported" and row["control"] == "emotion_intensity"
        for row in compiled.report
    )


def test_phrase_annotation_intensity_is_not_promoted_to_request_scope():
    compiled = compile_performance(
        "Hello. Bye.",
        {
            "service": "audio_cpp",
            "model": "index_tts2_q8_0",
            "_performance": {
                "decision": "steer",
                "delivery": {"emotion": "happy", "emotion_intensity": 0.5},
                "spans": [
                    {
                        "anchor": {"quote": "Bye."},
                        "delivery": {"emotion": "sad", "emotion_intensity": 1.0},
                    }
                ],
            },
        },
        {"id": "audio_cpp", "adapter": "audio_cpp"},
    )
    assert compiled.request_options["emotion_text"] == "happy"
    assert compiled.request_options["emotion_alpha"] == 0.5
    assert any(row["status"] == "unsupported" and row["control"] == "emotion" for row in compiled.report)
    assert any(
        row["status"] == "unsupported" and row["control"] == "emotion_intensity"
        for row in compiled.report
    )


def test_xml_intensity_roundtrip_preserves_inheritance_local_override_and_clear():
    text = "Before local reset neutral after"
    xml = (
        '<segment id="s"><em intensity="0">frustrated</em>Before '
        '<span><em intensity="0.4">annoyed</em>local</span> '
        '<span><em>calm</em>reset</span> '
        '<span><em/>neutral</span> after</segment>'
    )
    parsed = parse_speech_markup(xml, expected_segment_id="s", expected_text=text)
    assert [span.delivery.get("emotion_intensity") for span in parsed.spans] == [
        0.0, 0.4, 0.0, None, 0.0, None, 0.0
    ]
    assert [span.delivery["emotion"] for span in parsed.spans] == [
        "frustrated",
        "annoyed",
        "frustrated",
        "calm",
        "frustrated",
        "",
        "frustrated",
    ]
    assert parsed.transcript == text

    edited = edit_speech_markup_range(
        parsed.xml,
        segment_id="s",
        text=text,
        characters=None,
        start=text.index("reset"),
        end=text.index("reset") + len("reset"),
        delivery={"pace": "brisk"},
    )
    reparsed = parse_speech_markup(edited, expected_segment_id="s", expected_text=text)
    expected_deliveries = [span.delivery for span in parsed.spans]
    expected_deliveries[3] = {
        **expected_deliveries[3],
        "pace": "brisk",
    }
    assert [span.delivery for span in reparsed.spans] == expected_deliveries
    assert reparsed.transcript == text
    assert 'intensity="0"' in reparsed.xml


def test_selection_edit_clearing_emotion_clears_intensity_and_zero_is_kept():
    text = "Hello 👋!"
    xml = '<segment id="s"><em intensity="0.5">happy</em>Hello 👋!</segment>'
    cleared = edit_speech_markup_range(
        xml,
        segment_id="s",
        text=text,
        characters=None,
        start=0,
        end=len(text),
        delivery={"emotion": "", "emotion_intensity": 0.8},
    )
    parsed_clear = parse_speech_markup(
        cleared, expected_segment_id="s", expected_text=text
    )
    assert all(span.delivery["emotion"] == "" for span in parsed_clear.spans)
    assert all(span.delivery.get("emotion_intensity") is None for span in parsed_clear.spans)
    assert parsed_clear.transcript == text

    zero = edit_speech_markup_range(
        '<segment id="s"><em>happy</em>Hello 👋!</segment>',
        segment_id="s",
        text=text,
        characters=None,
        start=0,
        end=len(text),
        delivery={"emotion_intensity": 0},
    )
    parsed_zero = parse_speech_markup(
        zero, expected_segment_id="s", expected_text=text
    )
    assert all(span.delivery["emotion"] == "happy" for span in parsed_zero.spans)
    assert all(span.delivery["emotion_intensity"] == 0 for span in parsed_zero.spans)
    assert parsed_zero.transcript == text


@pytest.mark.parametrize(
    "attribute",
    ['intensity="nan"', 'intensity="inf"', 'intensity="2.01"', 'intensity="-0.1"'],
)
def test_xml_rejects_nonfinite_and_out_of_global_range_intensity(attribute):
    with pytest.raises(ValueError, match="intensity"):
        parse_speech_markup(
            f'<segment id="s"><em {attribute}>sad</em>Text</segment>',
            expected_segment_id="s",
            expected_text="Text",
        )


def test_xml_intensity_is_emotion_only_and_unknown_em_attributes_stay_rejected():
    with pytest.raises(ValueError, match="unknown attribute"):
        parse_speech_markup(
            '<segment id="s"><pace intensity="0.5">slow</pace>Text</segment>',
            expected_segment_id="s",
            expected_text="Text",
        )
    with pytest.raises(ValueError, match="non-empty emotion"):
        parse_speech_markup(
            '<segment id="s"><em intensity="0.5"/>Text</segment>',
            expected_segment_id="s",
            expected_text="Text",
        )


def test_native_intensity_changes_compiled_fingerprint_without_absent_default():
    without = _index({"emotion": "quiet delight"})
    with_intensity = _index(
        {"emotion": "quiet delight", "emotion_intensity": 0.5}
    )
    assert without.fingerprint != with_intensity.fingerprint
    assert "emotion_alpha" not in without.request_options
    assert "emotion_alpha" in with_intensity.request_options


def test_applied_emotion_intensity_roundtrips_through_selection_context(
    case, monkeypatch
):
    services = case["services"]
    segment_id = case["segment_ids"][0]
    text = "Zero Half Unset"
    with services["database"].session() as session:
        save_generation_controls(
            session,
            case["session_id"],
            expected_revision=0,
            characters=[
                {
                    "id": "alice",
                    "display_name": "Alice",
                    "delivery": {
                        "emotion": "gentle",
                        "emotion_intensity": 0.25,
                    },
                }
            ],
        )
        segment = session.get(m.GenerationSegment, segment_id)
        segment.text = text
        segment.speech_plan_json = {
            "speech_xml": f'<segment id="{segment_id}">{text}</segment>'
        }
        segment_revision = segment.revision

    candidate_xml = (
        f'<segment id="{segment_id}"><speaker ref="alice">'
        '<em intensity="0">calm</em>Zero '
        '<span><em intensity="0.5">calm</em>Half</span> '
        '<span><em>calm</em>Unset</span>'
        "</speaker></segment>"
    )
    request = {
        "revision_id": case["revision_id"],
        "segment_id": segment_id,
        "expected_segment_revision": segment_revision,
        "start": 0,
        "end": len(text),
        "speech_xml": candidate_xml,
        "enable_performance": True,
    }
    monkeypatch.setattr(speech_selection.plans, "preview_segment", lambda *_a, **_k: None)
    with services["database"].session() as session:
        preview = preview_speech_selection(
            services, session, case["session_id"], request
        )
    with services["database"].immediate_session() as session:
        apply_speech_selection(
            services,
            session,
            case["session_id"],
            {**request, "expected_preview_revision": preview["preview_revision"]},
        )

    monkeypatch.setattr(
        speech_selection,
        "preview_speech_segment_in_session",
        lambda *_a, **_k: None,
    )

    def context_for(start: int, end: int) -> dict:
        with services["database"].session() as session:
            segment = session.get(m.GenerationSegment, segment_id)
            return get_speech_selection_context(
                services,
                session,
                case["session_id"],
                {
                    "revision_id": case["revision_id"],
                    "segment_id": segment_id,
                    "expected_segment_revision": segment.revision,
                    "start": start,
                    "end": end,
                },
            )

    zero = context_for(0, 4)
    half = context_for(5, 9)
    unset = context_for(10, 15)
    mixed = context_for(0, len(text))

    assert zero["current"]["delivery"]["emotion"] == "calm"
    assert zero["current"]["delivery"]["emotion_intensity"] == 0
    assert half["current"]["delivery"]["emotion_intensity"] == 0.5
    assert unset["current"]["delivery"]["emotion_intensity"] is None
    assert mixed["current"]["delivery"]["emotion_intensity"] is None
    assert "emotion_intensity" in mixed["current"]["mixed_fields"]
    inherited = mixed["inherited"]["characters"]
    assert inherited[0]["delivery"]["emotion_intensity"] == 0.25


def test_selection_context_handles_absent_intensity_and_character_default(
    case, monkeypatch
):
    services = case["services"]
    segment_id = case["segment_ids"][0]
    text = "Calm text"
    xml = (
        f'<segment id="{segment_id}"><speaker ref="alice">'
        "<em>calm</em>Calm text</speaker></segment>"
    )
    with services["database"].session() as session:
        save_generation_controls(
            session,
            case["session_id"],
            expected_revision=0,
            characters=[
                {"id": "alice", "display_name": "Alice", "delivery": {"emotion": "gentle"}}
            ],
        )
        segment = session.get(m.GenerationSegment, segment_id)
        segment.text = text
        segment.speech_plan_json = {"speech_xml": xml}
        segment_revision = segment.revision

    monkeypatch.setattr(
        speech_selection,
        "preview_speech_segment_in_session",
        lambda *_a, **_k: None,
    )
    with services["database"].session() as session:
        context = get_speech_selection_context(
            services,
            session,
            case["session_id"],
            {
                "revision_id": case["revision_id"],
                "segment_id": segment_id,
                "expected_segment_revision": segment_revision,
                "start": 0,
                "end": len(text),
            },
        )

    assert context["current"]["delivery"]["emotion_intensity"] is None
    assert "emotion_intensity" not in context["current"]["mixed_fields"]
    assert context["inherited"]["characters"][0]["delivery"] == {
        "instruction": "",
        "emotion": "gentle",
        "pace": "",
        "cadence": "",
        "emphasis": "",
        "emotion_intensity": None,
    }
